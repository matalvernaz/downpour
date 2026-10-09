# PyInstaller build spec: one folder holding two executables.
#
#   Downpour.exe      the window (GUI subsystem, so no console ever flashes)
#   downpour-cli.exe  the command line, and the browser's native-messaging
#                     host - browsers talk to hosts over stdin and stdout,
#                     which only a console program is guaranteed to have
#
# A folder build rather than one file: the browser host starts on every
# captured download, and a one-file build would unpack itself to a temp
# folder each time.
#
# Build with build.cmd. UPX is off: it saves little and makes antivirus false
# positives more likely.

a = Analysis(
    ["downpour/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("downpour/sounds", "downpour/sounds"),
        ("downpour/vendor", "downpour/vendor"),
    ],
    hiddenimports=[
        "downpour.gui.app",
        "downpour.browser.native_host",
        "downpour.browser.install",
        "downpour.core.media",
    ],
    excludes=[
        "pytest",
        "_pytest",
        "tests",
        "tkinter",
        "unittest",
        "pydoc_data",
        "numpy",
        "PIL",
        "wx.lib.agw",
        "wx.py",
        "wx.tools",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Downpour",
    console=False,
    upx=False,
    version="build/version_info.txt",
)
cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="downpour-cli",
    console=True,
    upx=False,
    version="build/version_info.txt",
)
coll = COLLECT(gui, cli, a.binaries, a.datas, upx=False, name="Downpour")
