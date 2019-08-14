[![PyPi Release](https://img.shields.io/pypi/v/maestral.svg)](https://pypi.org/project/maestral/)
[![Pyversions](https://img.shields.io/pypi/pyversions/maestral.svg)](https://pypi.org/pypi/maestral/)

# Maestral <img src="https://raw.githubusercontent.com/SamSchott/maestral-dropbox/master/maestral/gui/resources/Maestral.png" align="right" title="Maestral" width="110" height="110">

A light-weight and open-source Dropbox client for macOS and Linux.

## About

Maestral is an open-source Dropbox client written in Python. The project's main goal is to
provide a client for platforms and file systems that are no longer directly supported by
Dropbox.

Currently, Maestral does not support Dropbox Paper, the management of Dropbox teams and
the management of shared folder settings. If you need any of this functionality, please
use the Dropbox website or the official client.

The focus on "simple" file syncing does come with advantages: the Maestral App on macOS is
80% smaller than the official Dropbox app (50 MB vs 290 MB) and uses 70% less memory. The
app size and memory footprint can be further reduced when installing and running Maestral
without a GUI and using the Python installation provided by your OS. The Maestral code
itself and its Python dependencies take up less than 3 MB,  making an install without GUI
ideal for systems with little resources.

In the latest beta, Maestral introduces experimental support for multiple Dropbox accounts.

## Installation

A binary is provided for macOS High Sierra and higher and can be downloaded from the
Releases tab. On other platforms, download and install the Python package from PyPI:
```console
$ python3 -m pip install --upgrade maestral
```
You can also install the latest beta:
```console
$ python3 -m pip install --upgrade --pre maestral
```
If you intend to use the graphical user interface, you also need to install PyQt5, either
from PyPI or form your platforms package manager.

## Usage

Run `maestral gui` in the command line (or open the Maestral app on macOS) to start
Maestral with a graphical user interface. On its first run, Maestral will guide you
through linking and configuring your Dropbox and will then start syncing.

![screenshot macOS](https://raw.githubusercontent.com/SamSchott/maestral-dropbox/master/screenshots/macOS.png)
![screenshot Fedora](https://raw.githubusercontent.com/SamSchott/maestral-dropbox/master/screenshots/Ubuntu.png)

## Command line usage

After installation, Maestral will be available as a command line script by typing
`maestral` in the command prompt. Command line functionality resembles that of the
interactive client. Type `maestral --help` to get a full list of available commands. The
most important are:

- `maestral gui`: Starts Maestral with a GUI.
- `maestral daemon {start/stop}`: Start or stop Maestral as a daemon.
- `maestral daemon {pause/resume}`: Pause or resume syncing.
- `maestral daemon status`: Get the current sync status.
- `maestral daemon errors`: Lists all sync errors.
- `maestral set-dir`: Sets the location of your local Dropbox folder.
- `maestral dir-exclude`: Excludes a Dropbox folder from syncing.
- `maestral dir-inlcude`: Includes a Dropbox folder in syncing.
- `maestral list`: Lists the contents of a directory on Dropbox.

Maestral supports syncing multiple Dropbox accounts by running multiple instances. For
now, the configuration needs to be done from the command line. E.g., before running
`maestral gui`, one can set up a new configuration with `maestral config new`. For
instance, to sync both a private and business account, run:

```shell
$ maestral config new "personal"
$ maestral config new "work"
$ maestral gui --config-name="personal"
$ maestral gui --config-name="work"
```
This will start two Maestral instances, syncing the private and the work accounts,
respectively. By default, the Dropbox folder names will contain the capitalised
config-name in braces. For our example, this will be "Dropbox (Personal)" and "Dropbox
(Work)".

## Contribute

The following tasks could need your help:

- [ ] Write tests for maestral.
- [ ] Detect and warn in case of unsupported Dropbox folder locations (network drives,
      external hard drives, etc).
- [ ] Speed up downloads of large folders and initial sync: Download zip files if possible.
- [ ] Native Cocoa and GTK interfaces. Maestral currently uses PyQt5.
- [ ] Packaging: improve packing for macOS (reduce app size) and package for other platforms.

## Warning:

- Maestral is still in beta status. Even through highly unlikely, using it may potentially
  result in loss of data.
- Network drives and some external hard drives are not supported as locations for the
  Dropbox folder.

## Dependencies

*System:*
- macOS or Linux
- Python 3.6 or higher
- [gnome-shell-extension-appindicator](https://github.com/ubuntu/gnome-shell-extension-appindicator)
  on Gnome 3.26 and higher
- PyQt 5.9 or higher (for GUI only).

*Python:*
- click
- dropbox
- watchdog
- blinker
- requests
- u-msgpack-python
- keyring
- keyring.alt

# Acknowledgements

- The config module uses code from the [Spyder IDE](https://github.com/spyder-ide).
- The MaestralApiClient is based on the work from [Orphilia](https://github.com/ksiazkowicz/orphilia-dropbox).
