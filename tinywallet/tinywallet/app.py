"""
Copyright (c) 2019, Brian Stafford
Copyright (c) 2019, the Decred developers
See LICENSE for details

A PyQt light wallet.
"""

import os
import sys

from PyQt5 import QtCore, QtGui, QtWidgets

from decred.dcr import constants as DCR
from decred.dcr.dcrdata import DcrdataBlockchain
from decred.util import chains, database, helpers
from decred.wallet.wallet import Wallet
from tinywallet import config, qutilities as Q, screens, ui
from tinywallet.config import DB


# the directory of the tinywallet package
PACKAGEDIR = os.path.dirname(os.path.realpath(__file__))

# some commonly used ui constants
TINY = ui.TINY
SMALL = ui.SMALL
MEDIUM = ui.MEDIUM
LARGE = ui.LARGE

# a filename for the wallet
WALLET_FILE_NAME = "wallet.db"

formatTraceback = helpers.formatTraceback


class TinySignals:
    """
    Implements the Signals API as defined in tinydecred.api. TinySignals is used
    by the Wallet to broadcast notifications.
    """

    def __init__(self, balance=None, working=None, done=None, spentTickets=None):
        """
        Args:
            balance (func(Balance)): A function to receive balance updates.
                Updates are broadcast as an object implementing the Balance API.
        """
        dummy = lambda *a, **k: None
        self.balance = balance if balance else dummy
        self.working = working if working else dummy
        self.done = done if done else dummy
        self.spentTickets = spentTickets if spentTickets else dummy


class TinyWallet(QtCore.QObject, Q.ThreadUtilities):
    """
    TinyWallet is an PyQt application for interacting with the Decred
    blockchain. TinyWallet currently implements a UI for creating and
    controlling a rudimentary, non-staking, Decred testnet light wallet.

    TinyWallet is a system tray application.
    """

    qRawSignal = QtCore.pyqtSignal(tuple)
    homeSig = QtCore.pyqtSignal(Q.PyObj)
    walletSig = QtCore.pyqtSignal(Q.PyObj)

    def __init__(self, qApp):
        """
        Args:
            qApp (QApplication): An initialized QApplication.
        """
        super().__init__()
        self.qApp = qApp
        self.cfg = config.load()
        self.log = self.initLogging()
        self.wallet = None
        # trackedCssItems are CSS-styled elements to be updated if dark mode is
        # enabled/disabled.
        self.trackedCssItems = []
        st = self.sysTray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(DCR.FAVICON))
        self.contextMenu = ctxMenu = QtWidgets.QMenu()
        ctxMenu.addAction("minimize").triggered.connect(self.minimizeApp)
        ctxMenu.addAction("quit").triggered.connect(lambda *a: self.qApp.quit())
        st.setContextMenu(ctxMenu)
        st.activated.connect(self.sysTrayActivated)

        # The signalRegistry maps a signal to any number of receivers. Signals
        # are routed through a Qt Signal.
        self.signalRegistry = {}
        self.qRawSignal.connect(self.signal_)
        self.blockchainSignals = TinySignals(
            balance=self.balanceSync,
            working=lambda: self.emitSignal(ui.WORKING_SIGNAL),
            done=lambda: self.emitSignal(ui.DONE_SIGNAL),
            spentTickets=lambda: self.emitSignal(ui.SPENT_TICKETS_SIGNAL),
        )

        self.netDirectory = os.path.join(config.DATA_DIR, self.cfg.netParams.Name)

        helpers.mkdir(self.netDirectory)
        self.appDB = database.KeyValueDatabase(
            os.path.join(self.netDirectory, "app.db")
        )
        self.settings = self.appDB.child("settings")
        self.loadSettings()

        dcrdataDB = database.KeyValueDatabase(os.path.join(self.netDirectory, "dcr.db"))
        # The initialized DcrdataBlockchain will not be connected, as that is a
        # blocking operation. It will be called when the wallet is open.
        self.dcrdata = DcrdataBlockchain(
            dcrdataDB,
            self.cfg.netParams,
            self.settings[DB.dcrdata].decode(),
            skipConnect=True,
        )
        chains.registerChain("dcr", self.dcrdata)

        # appWindow is the main application window. The TinyDialog class has
        # methods for organizing a stack of Screen widgets.
        self.appWindow = screens.TinyDialog(self)

        self.homeSig.connect(self.home_)

        def gohome(screen=None):
            self.homeSig.emit(screen)

        self.home = gohome
        self.homeScreen = None

        self.pwDialog = screens.PasswordDialog()

        self.waitingScreen = screens.WaitingScreen()
        # Set waiting screen as initial home screen.
        self.appWindow.stack(self.waitingScreen)

        self.confirmScreen = screens.ConfirmScreen()

        self.walletSig.connect(self.setWallet_)

        def setwallet(wallet):
            self.walletSig.emit(wallet)

        self.setWallet = setwallet

        self.sysTray.show()
        self.appWindow.show()

        self.initialize()

    def initLogging(self):
        """
        Initialize logging for the entire app.
        """
        logDir = os.path.join(config.DATA_DIR, "logs")
        helpers.mkdir(logDir)
        logFilePath = os.path.join(logDir, "tinydecred.log")
        helpers.prepareLogging(
            logFilePath, logLvl=self.cfg.logLevel, lvlMap=self.cfg.moduleLevels
        )
        log = helpers.getLogger("APP")
        log.info("configuration file at %s" % config.CONFIG_PATH)
        log.info("data directory at %s" % config.DATA_DIR)
        return log

    def initialize(self):
        """
        Show the initial screen based on the presence of a wallet file.
        """
        # If there is a wallet file, prompt for a password to open the wallet.
        # Otherwise, show the initialization screen.
        path = self.walletFilename()
        if os.path.isfile(path):

            try:
                self.dcrdata.connect()
                w = Wallet(path)
                self.setWallet(w)
                self.home(self.assetScreen)
            except Exception as e:
                self.log.warning(
                    "exception encountered while attempting to initialize wallet: %s"
                    % formatTraceback(e)
                )
                self.appWindow.showError("error opening wallet")

        else:
            initScreen = screens.InitializationScreen()
            initScreen.setFadeIn(True)
            self.appWindow.stack(initScreen)

    def waiting(self):
        """
        Stack the waiting screen.
        """
        self.appWindow.stack(self.waitingScreen)

    def waitThread(self, f, cb, *a, **k):
        """
        Wait thread shows a waiting screen while the provided function is run
        in a separate thread.

        Args:
            f (func): A function to run in a separate thread.
            cb (func): A callback to receive the return values from f.
            *args (tuple): Positional arguments passed to f.
            **kwargs (dict): Keyword arguments passed directly to f.
        """
        cb = cb if cb else lambda *a, **k: None
        self.waiting()

        def run():
            try:
                return f(*a, **k)
            except Exception as e:
                err_msg = "waitThread execution error {} failed: {}"
                self.log.error(err_msg.format(f.__name__, formatTraceback(e)))
            finally:
                self.appWindow.pop(self.waitingScreen)

        self.makeThread(run, cb)

    def getPassword(self, f, *args, **kwargs):
        """
        Calls the provided function with a user-provided password string as its
        first argument. Any additional arguments provided to getPassword are
        appended as-is to the password argument.

        Args:
            f (func): A function that will receive the user's password
                and any other provided arguments.
            *args (tuple): Positional arguments passed to f. The position
                of the args will be shifted by 1 position with the user's
                password inserted at position 0.
            **kwargs (dict): Keyword arguments passed directly to f.
        """
        self.appWindow.stack(self.pwDialog.withCallback(f, *args, **kwargs))

    def walletFilename(self):
        return self.settings[DB.wallet].decode()

    def sysTrayActivated(self, trigger):
        """
        Qt Slot called when the user interacts with the system tray icon. Shows
        the window, creating an icon in the user's application panel that
        persists until the appWindow is minimized.
        """
        if trigger == QtWidgets.QSystemTrayIcon.Trigger:
            self.appWindow.show()
            self.appWindow.activateWindow()

    def minimizeApp(self, *a):
        """
        Minimizes the application. Because TinyWallet is a system-tray app, the
        program does not halt execution, but the icon is removed from the
        application panel. Any arguments are ignored.
        """
        self.appWindow.close()
        self.appWindow.hide()

    def loadSettings(self):
        """
        Load settings from the settings table.
        """
        if DB.theme not in self.settings:
            self.settings[DB.theme] = Q.LIGHT_THEME.encode()
        if DB.wallet not in self.settings:
            self.settings[DB.wallet] = os.path.join(
                self.netDirectory, WALLET_FILE_NAME
            ).encode()
        if DB.dcrdata not in self.settings:
            self.settings[DB.dcrdata] = config.NetworkDefaults[self.cfg.netParams.Name][
                "dcrdata"
            ].encode()

    def registerSignal(self, sig, cb, *a, **k):
        """
        Register the receiver with the signal registry.

        The callback arguments will be preceeded with any signal-specific
        arguments. For example, the BALANCE_SIGNAL will have `balance (float)`
        as its first argument, followed by unpacking *a.

        Args:
            sig (str): A notification identifier registered with the
                signalRegistry.
            cb (func): Consumer defined callback.
            *a (tuple): Positional arguments passed to cb.
            **k (dict): Keyword arguments passed directly to cb.
        """
        if sig not in self.signalRegistry:
            self.signalRegistry[sig] = []
        # Elements at indices 1 and 3 are set when emitted.
        self.signalRegistry[sig].append((cb, [], a, {}, k))

    def emitSignal(self, sig, *sigA, **sigK):
        """
        Emit a notification of type `sig`.

        Args:
            sig (str): A notification identifier registered with the
                signalRegistry.
            *sigA (tuple): Positional arguments passed to cb.
            **sigK (dict): Keyword arguments passed directly to cb.
        """
        sr = self.signalRegistry
        if sig not in sr:
            self.log.warning("attempted to call un-registered signal %s" % sig)
            return
        for s in sr[sig]:
            sa, sk = s[1], s[3]
            sa.clear()
            sa.extend(sigA)
            sk.clear()
            sk.update(sigK)
            self.qRawSignal.emit(s)

    def signal_(self, s):
        """
        A Qt Slot used for routing signalRegistry signals.

        Args:
            s (tuple): A tuple of (func, signal args, user args, signal kwargs,
                user kwargs).
        """
        cb, sigA, a, sigK, k = s
        cb(*sigA, *a, **sigK, **k)

    def setWallet_(self, wallet):
        """
        Set the current wallet.

        Args:
            wallet (Wallet): The wallet to use.
        """
        self.wallet = wallet
        self.assetScreen = screens.AssetScreen()

    def confirm(self, msg, cb):
        """
        Call the callback function only if the user confirms the prompt.
        """
        self.appWindow.stack(self.confirmScreen.withPurpose(msg, cb))

    def balanceSync(self, balance):
        """
        A Signal method for the wallet. Emits the BALANCE_SIGNAL.

        Args:
            balance (Balance): The balance to pass to subscribed receivers.
        """
        self.emitSignal(ui.BALANCE_SIGNAL, balance)

    def getButton(self, size, text, tracked=True):
        """
        Get a button of the requested size.
        Size can be one of [TINY, SMALL, MEDIUM, LARGE].
        The button is assigned a style in accordance with the current template.
        By default, the button is tracked and appropriately updated if the
        template is updated.

        Args:
            size (str): One of [TINY, SMALL, MEDIUM, LARGE].
            text (str): The text displayed on the button.
            tracked (bool): default True. Whether to track the button. If it's
                a one time use button, as for a dynamically generated dialog,
                the button should not be tracked.

        Returns:
            QPushButton: An initilized Qt pushable button.
        """
        button = QtWidgets.QPushButton(text, self.appWindow)
        button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        if self.settings[DB.theme].decode() == Q.LIGHT_THEME:
            button.setProperty("button-style-class", Q.LIGHT_THEME)
        if size == TINY:
            button.setProperty("button-size-class", TINY)
        elif size == SMALL:
            button.setProperty("button-size-class", SMALL)
        elif size == MEDIUM:
            button.setProperty("button-size-class", MEDIUM)
        elif size == LARGE:
            button.setProperty("button-size-class", LARGE)
        if tracked:
            self.trackedCssItems.append(button)
        return button

    def home_(self, screen=None):
        """
        Go to the home screen or set a new home screen.

        Args:
            screen (Screen | tuple(Screen)): Behavior will differ depending on
                type passed. If no screen is passed, the last home screen will
                be used. If screen is a Screen, it will be set as the new home
                screen. If screen is a tuple of Screen, The first Screen in the
                tuple will be set to the home screen, and the rest will be
                stacked.
        """
        stacks = tuple()
        if isinstance(screen, tuple):
            stacks = screen[1:]
            screen = screen[0]
        if screen:
            self.homeScreen = screen
        self.appWindow.setHomeScreen(self.homeScreen)
        for stack in stacks:
            self.appWindow.stack(stack)

    def showMnemonics(self, words):
        """
        Show the mnemonic key. Persists until the user indicates completion.

        Args:
            list(str): List of mnemonic words.
        """
        screen = screens.MnemonicScreen(words)
        self.home((self.assetScreen, screen))


def loadFonts():
    """
    Load the application font files.
    """
    # see https://github.com/google/material-design-icons/blob/master/iconfont/codepoints
    # for conversions to unicode
    # http://zavoloklom.github.io/material-design-iconic-font/cheatsheet.html
    for filename in os.listdir(ui.FONTDIR):
        if filename.endswith(".ttf"):
            QtGui.QFontDatabase.addApplicationFont(os.path.join(ui.FONTDIR, filename))


# Some issues' responses have indicated that certain exceptions may not be
# displayed when Qt crashes unless this excepthook redirection is used.
sys._excepthook = sys.excepthook


def exception_hook(exctype, value, tb):
    """
    Helper function to explicitly print uncaught QT exceptions.

    Args:
        exctype (Exception): The exception Class.
        value (value): The exception instance.
        tb (Traceback): The exception traceback.
    """
    print(exctype, value, tb)
    sys._excepthook(exctype, value, tb)
    sys.exit(1)


def main():
    """
    Start the TinyWallet application.
    """
    sys.excepthook = exception_hook
    QtWidgets.QApplication.setDesktopSettingsAware(False)
    roboFont = QtGui.QFont("Roboto")
    roboFont.setPixelSize(16)
    QtWidgets.QApplication.setFont(roboFont)
    qApp = QtWidgets.QApplication(sys.argv)
    qApp.setStyleSheet(Q.QUTILITY_STYLE)
    qApp.setPalette(Q.lightThemePalette)
    qApp.setWindowIcon(QtGui.QIcon(DCR.LOGO))
    qApp.setApplicationName("Tiny Decred")
    loadFonts()

    decred = TinyWallet(qApp)
    try:
        qApp.exec_()
    except Exception as e:
        print(formatTraceback(e))
    decred.sysTray.hide()
    qApp.deleteLater()
    return


if __name__ == "__main__":
    main()
