# -*- mode: python ; coding: utf-8 -*-

block_cipher = None


from maestral import __version__


a = Analysis(['maestral/gui/main.py'],
             binaries=[],
             datas= [
                ('maestral/gui/resources/tray-icons-svg/*.svg', './tray-icons-svg'),
                ('maestral/gui/resources/maestral.png', '.'),
                ('maestral/gui/resources/faceholder.png', '.'),
                ('maestral/gui/resources/*.ui', '.')
             ],
             hiddenimports=[],
             hookspath=[],
             runtime_hooks=[],
             excludes=['_tkinter'],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)
pyz = PYZ(a.pure, a.zipped_data,
             cipher=block_cipher)
exe = EXE(pyz,
          a.scripts,
          [],
          exclude_binaries=True,
          name='main',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          console=False )
coll = COLLECT(exe,
               a.binaries,
               a.zipfiles,
               a.datas,
               strip=False,
               upx=True,
               upx_exclude=[],
               name='main')
app = BUNDLE(coll,
             name='Maestral.app',
             icon='maestral/gui/resources/maestral.icns',
             bundle_identifier='com.samschott.maestral',
             info_plist={
                'NSHighResolutionCapable': 'True',
                'NSRequiresAquaSystemAppearance': 'False',
                'LSUIElement': '1',
                'CFBundleVersion': '57',
                'CFBundleShortVersionString': __version__,
                'NSHumanReadableCopyright': 'Copyright © 2019 Sam Schott. All rights reserved.',
                'LSMinimumSystemVersion': '10.13.0',
                },
)
