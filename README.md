signal-media-exporter
=====================

Python script to export media files from [Signal Desktop](https://github.com/signalapp/Signal-Desktop).

The Signal Desktop client stores attachment files in the `$HOME/.config/Signal/attachments.noindex` folder. This script opens the encrypted SQLite database to gather the associated metadata (sender number, message timestamp and expiration date, media content type) and copies the media files to the configured export directory, grouped by the sender number/name. It can be run without needing to exit the Signal Desktop client and will only process newly received media files when run in regular intervals.

Features
--------

* export media files to a specified output directory
* either export files from all senders (by number) or from selected senders only (by assigning names)
* file name and modification time will be set to the timestamp of the original message
* only newly received media files will be processed on repeated runs - but if you decide to add or rename a selected sender at a later time, all missing media files will be exported on the next run
* if the same media file appears in multiple conversations, only the earliest instance will be exported (deduplication)
* media files from expiring messages are not exported by default

Requirements
------------

### libsqlcipher

`libsqlcipher>=3.30` must be installed on your system prior to installation of `pysqlcipher`, so that the binary extension module can be compiled. Consult your operating system documentation for how to install SQL Cipher. You can also manually build SQL Cipher by cloning [sqlcipher](https://github.com/sqlcipher/sqlcipher) and following the build instructions, followed by `sudo make install && sudo ldconfig`.

For Arch Linux, you can install the [sqlcipher](https://www.archlinux.org/packages/community/x86_64/sqlcipher/) package. For Debian, there is a (currently outdated) [libsqlcipher-dev](https://packages.debian.org/stable/libsqlcipher-dev) package.

### Python (>=3.6)

* [pysqlcipher3](https://github.com/rigglemania/pysqlcipher3) (via pip)
* [coloredlogs](https://github.com/xolox/python-coloredlogs) (via pip)

If you have poetry installed, you can run:

```
$ poetry install
$ poetry run signal-media-exporter -h
```

Otherwise, using only pip, you can create a virtalenv:

```
$ python3 -m venv venv
$ source venv/bin/activate
(venv) $ pip install -r requirements.txt
(venv) $ python -m signal_media_exporter -h
```

And to install for use from outside the project's directory:

```
$ poetry build
$ python3 -m pip install dist/*.tar.gz
$ signal_media_exporter -h
```

Usage / Configuration
---------------------

You can either just use the similar command line parameters (see `signal-media-exporter -h`) or create a `config.json` file by modifying the supplied `config.json.example`:

```
{
    "outputDir": "./media",
    "includeExpiringMessages": false,
    "map": {
        "+430000000000": "Me",
        "+430000000001": "My buddy"
    }
}
```

* `outputDir`: Output directory for media files, as a relative or absolute path. Inside this directory, subdirectories for the sender numbers or names will be created.
* `includeExpiringMessages`: Whether to include media files from messages that are set to expire in the future.
* `map`: If you include this dict, only the media files sent by the listed numbers will be exported, and the supplied name will be used for the `outputDir` subdirectories (including your own number). If omitted, all media files will be exported using the senders' numbers as subdirectories. Phone numbers must be complete including the country code.
