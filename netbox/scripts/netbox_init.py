#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2024 Battelle Energy Alliance, LLC.  All rights reserved.

import argparse
import ast
import glob
import gzip
import ipaddress
import itertools
import json
import logging
import magic
import os
import psycopg
import pynetbox
import randomcolor
import re
import shutil
import sys
import tarfile
import tempfile
import time
import malcolm_utils

from collections.abc import Iterable
from distutils.dir_util import copy_tree
from datetime import datetime
from packaging.version import Version
from slugify import slugify

###################################################################################################
args = None
script_name = os.path.basename(__file__)
script_path = os.path.dirname(os.path.realpath(__file__))
orig_path = os.getcwd()


###################################################################################################
def min_hash_value_by_value(x):
    return next(
        iter(list({k: v for k, v in sorted(x.items(), key=lambda item: item[1])}.values())),
        None,
    )


def min_hash_value_by_key(x):
    return next(
        iter(list({k: v for k, v in sorted(x.items(), key=lambda item: item[0])}.values())),
        None,
    )


def max_hash_value_by_value(x):
    try:
        *_, last = iter(list({k: v for k, v in sorted(x.items(), key=lambda item: item[1])}.values()))
    except Exception:
        last = None
    return last


def max_hash_value_by_key(x):
    try:
        *_, last = iter(list({k: v for k, v in sorted(x.items(), key=lambda item: item[0])}.values()))
    except Exception:
        last = None
    return last


def GetInstalledPackages(venvPy):
    packagesInstalled = {}
    cmd = [
        venvPy,
        "-m",
        "pip",
        "--no-color",
        "--no-input",
        "--disable-pip-version-check",
        "list",
        "--local",
        "--format",
        "json",
        "--verbose",
    ]
    err, results = malcolm_utils.run_process(cmd, stderr=False, logger=logging)
    if (err == 0) and results and (len(results) > 0):
        try:
            packagesInstalled = {item['name']: item for item in malcolm_utils.LoadStrIfJson(results[0])}
        except Exception as e:
            logging.error(f"{type(e).__name__} getting list of installed Python packages: {e}")

    return packagesInstalled


def InstallPackageDirIfNeeded(
    packageDir,
    venvPy,
    preinstalledPackagesDict={},
):
    installResult = False

    # First do a "dry run" install to determine what would happen. The report from this will
    #   help us determine if the package actually needs installed or not, as pip always treats
    #   installations from local directories as "new installs" and would uninstall/reinstall
    #   no matter what, which we want to avoid if we don't need it.
    pluginNeedsInstall = False
    with malcolm_utils.temporary_filename(suffix='.json') as dryRunInstallReportFileName:
        cmd = [
            venvPy,
            "-m",
            "pip",
            "--no-color",
            "--no-input",
            "--disable-pip-version-check",
            "install",
            "--upgrade",
            "--dry-run",
            "--progress-bar",
            "off",
            "--report",
            dryRunInstallReportFileName,
            packageDir,
        ]
        err, results = malcolm_utils.run_process(cmd, logger=logging)
        if (err == 0) and os.path.isfile(dryRunInstallReportFileName):
            with open(dryRunInstallReportFileName, 'r') as f:
                dryRunReport = malcolm_utils.LoadFileIfJson(f)
                wouldInstallInfo = {
                    malcolm_utils.deep_get(installItem, ['metadata', 'name']): malcolm_utils.deep_get(
                        installItem, ['metadata', 'version']
                    )
                    for installItem in dryRunReport.get('install', [])
                }
            pluginNeedsInstall = any(
                [
                    package_name
                    for package_name, new_version in wouldInstallInfo.items()
                    if (package_name not in preinstalledPackagesDict)
                    or (Version(new_version) > Version(preinstalledPackagesDict[package_name]['version']))
                ]
            )
        else:
            pluginNeedsInstall = True

    if pluginNeedsInstall:
        with malcolm_utils.temporary_filename(suffix='.json') as installReportFileName:
            cmd = [
                venvPy,
                "-m",
                "pip",
                "--no-color",
                "--no-input",
                "--disable-pip-version-check",
                "install",
                "--upgrade",
                "--progress-bar",
                "off",
                "--report",
                installReportFileName,
                packageDir,
            ]
            err, results = malcolm_utils.run_process(cmd, logger=logging)
            installResult = err == 0

    return installResult


###################################################################################################
# main
def main():
    global args

    parser = argparse.ArgumentParser(
        description='\n'.join([]),
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False,
        usage='{} <arguments>'.format(script_name),
    )
    parser.add_argument(
        '--verbose',
        '-v',
        action='count',
        default=1,
        help='Increase verbosity (e.g., -v, -vv, etc.)',
    )
    parser.add_argument(
        '--wait',
        dest='wait',
        action='store_true',
        help='Wait for connection first',
    )
    parser.add_argument(
        '--no-wait',
        dest='wait',
        action='store_false',
        help='Do not wait for connection (error if connection fails)',
    )
    parser.set_defaults(wait=True)
    parser.add_argument(
        '-u',
        '--url',
        dest='netboxUrl',
        type=str,
        default='http://localhost:8080/netbox',
        required=False,
        help="NetBox Base URL",
    )
    parser.add_argument(
        '-t',
        '--token',
        dest='netboxToken',
        type=str,
        default=None,
        required=False,
        help="NetBox API Token",
    )
    parser.add_argument(
        '-s',
        '--site',
        dest='netboxSites',
        nargs='*',
        type=str,
        default=[os.getenv('NETBOX_DEFAULT_SITE', 'default')],
        required=False,
        help="Site(s) to create",
    )
    parser.add_argument(
        '--default-group',
        dest='defaultGroupName',
        type=str,
        default=os.getenv('REMOTE_AUTH_DEFAULT_GROUPS', 'standard'),
        required=False,
        help="Name of default group for automatic NetBox user creation",
    )
    parser.add_argument(
        '--staff-group',
        dest='staffGroupName',
        type=str,
        default=os.getenv('REMOTE_AUTH_STAFF_GROUPS', 'administrator'),
        required=False,
        help="Name of staff group for automatic NetBox user creation",
    )
    parser.add_argument(
        '-m',
        '--manufacturer',
        dest='manufacturers',
        nargs='*',
        type=str,
        default=[os.getenv('NETBOX_DEFAULT_MANUFACTURER', 'Unspecified')],
        required=False,
        help="Manufacturers to create",
    )
    parser.add_argument(
        '-r',
        '--role',
        dest='roles',
        nargs='*',
        type=str,
        default=[os.getenv('NETBOX_DEFAULT_ROLE', 'Unspecified')],
        required=False,
        help="Role(s) to create",
    )
    parser.add_argument(
        '-y',
        '--device-type',
        dest='deviceTypes',
        nargs='*',
        type=str,
        default=[os.getenv('NETBOX_DEFAULT_DEVICE_TYPE', 'Unspecified')],
        required=False,
        help="Device types(s) to create",
    )
    parser.add_argument(
        '-n',
        '--netbox',
        dest='netboxDir',
        type=str,
        default=os.getenv('NETBOX_PATH', '/opt/netbox'),
        required=False,
        help="NetBox installation directory",
    )
    parser.add_argument(
        '--netbox-config',
        dest='netboxConfigDir',
        type=str,
        default=os.getenv('NETBOX_CONFIG_PATH', '/etc/netbox/config'),
        required=False,
        help="NetBox config directory (containing plugins.py, etc.)",
    )
    parser.add_argument(
        '-l',
        '--library',
        dest='libraryDir',
        type=str,
        default=os.getenv('NETBOX_DEVICETYPE_LIBRARY_IMPORT_PATH', '/opt/netbox-devicetype-library-import'),
        required=False,
        help="Directory containing NetBox Device-Type-Library-Import project and library repo",
    )
    parser.add_argument(
        '-c',
        '--custom-plugins',
        dest='customPluginsDir',
        type=str,
        default=os.getenv('NETBOX_CUSTOM_PLUGINS_PATH', '/opt/netbox-custom-plugins'),
        required=False,
        help="Parent directory containing custom NetBox plugins to install",
    )
    parser.add_argument(
        '-p',
        '--preload',
        dest='preloadDir',
        type=str,
        default=os.getenv('NETBOX_PRELOAD_PATH', '/opt/netbox-preload'),
        required=False,
        help="Directory containing netbox-initializers files to preload",
    )
    parser.add_argument(
        '--preload-backup',
        dest='preloadBackupFile',
        type=str,
        default=os.getenv('NETBOX_PRELOAD_GZ', default=''),
        required=False,
        help="Database dump .gz file to preload into postgreSQL",
    )
    parser.add_argument(
        '--postgres-host',
        dest='postgresHost',
        type=str,
        default=os.getenv('DB_HOST', 'netbox-postgres'),
        required=False,
        help="postgreSQL host for preloading an entire database dump .gz (specified with --preload-backup or loaded from the --preload directory)",
    )
    parser.add_argument(
        '--postgres-db',
        dest='postgresDB',
        type=str,
        default=os.getenv('DB_NAME', 'netbox'),
        required=False,
        help="postgreSQL database name",
    )
    parser.add_argument(
        '--postgres-user',
        dest='postgresUser',
        type=str,
        default=os.getenv('DB_USER', 'netbox'),
        required=False,
        help="postgreSQL user name",
    )
    parser.add_argument(
        '--postgres-password',
        dest='postgresPassword',
        type=str,
        default=os.getenv('DB_PASSWORD', ''),
        required=False,
        help="postgreSQL password",
    )
    try:
        parser.error = parser.exit
        args = parser.parse_args()
    except SystemExit:
        parser.print_help()
        exit(2)

    args.verbose = logging.ERROR - (10 * args.verbose) if args.verbose > 0 else 0
    logging.basicConfig(
        level=args.verbose, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    )
    logging.debug(os.path.join(script_path, script_name))
    logging.debug("Arguments: {}".format(sys.argv[1:]))
    logging.debug("Arguments: {}".format(args))
    if args.verbose > logging.DEBUG:
        sys.tracebacklimit = 0

    netboxVenvPy = os.path.join(os.path.join(os.path.join(args.netboxDir, 'venv'), 'bin'), 'python')
    manageScript = os.path.join(os.path.join(args.netboxDir, 'netbox'), 'manage.py')

    # CUSTOM PLUGIN INSTALLATION #################################################################################
    if os.path.isdir(args.customPluginsDir) and os.path.isfile(os.path.join(args.netboxConfigDir, 'plugins.py')):

        # get a list of what packages/plugins already installed (package names and versions in a dict)
        packagesInstalled = GetInstalledPackages(netboxVenvPy)

        # if there is a "requirements" subdirectory, handle that first as it contains dependencies
        if os.path.isdir(os.path.join(args.customPluginsDir, 'requirements')):
            requirementsSubDirs = [
                malcolm_utils.remove_suffix(f.path, '/')
                for f in os.scandir(os.path.join(args.customPluginsDir, 'requirements'))
                if f.is_dir()
            ]
            for packageDir in requirementsSubDirs:
                packageInstalled = InstallPackageDirIfNeeded(packageDir, netboxVenvPy, packagesInstalled)
                logging.info(
                    f"{os.path.basename(packageDir)} (dependency): {'' if packageInstalled else 'not ' }installed"
                )

        # now install the plugins directories
        installedOrUpdatedPlugins = []
        customPluginSubdirs = [
            malcolm_utils.remove_suffix(f.path, '/')
            for f in os.scandir(args.customPluginsDir)
            if f.is_dir() and (os.path.basename(f) != 'requirements')
        ]
        for pluginDir in customPluginSubdirs:
            if pluginInstalled := InstallPackageDirIfNeeded(pluginDir, netboxVenvPy, packagesInstalled):
                installedOrUpdatedPlugins.append(pluginDir)
            logging.info(f"{os.path.basename(pluginDir)}: {'' if pluginInstalled else 'not ' }installed")

        # for any packages that were newly installed (or updated, we'll be thorough) we need to make
        #   sure the package name is in the plugins.py
        logging.info(f"Plugins installed or updated: {installedOrUpdatedPlugins}")
        if installedOrUpdatedPlugins:
            # get updated list of installed packages
            packagesInstalled = GetInstalledPackages(netboxVenvPy)

        # now get the names of the NetBox plugins installed
        pluginNames = []

        # first get a list of __init__.py files for potential plugins installed in the package location(s)
        cmd = [
            '/usr/bin/rg',
            '--files-with-matches',
            '--iglob',
            '__init__.py',
            r'\bPluginConfig\b',
            list({package['location'] for package in packagesInstalled.values() if 'location' in package}),
        ]
        err, results = malcolm_utils.run_process(cmd, stderr=False, logger=logging)
        if results:
            # process each of those potential plugin __init__.py files
            for pluginInitFileName in results:
                try:
                    if os.path.isfile(pluginInitFileName):
                        # parse the Python of the __init__.py into an abstract syntax tree
                        with open(pluginInitFileName, 'r') as f:
                            node = ast.parse(f.read())
                            # look at each Class defined in this code
                            for c in [n for n in node.body if isinstance(n, ast.ClassDef)]:
                                # plugins are classes with "PluginConfig" for a parent
                                if any([baseClass.id == 'PluginConfig' for baseClass in c.bases]):
                                    # this ia a plugin class, so iterate over its members (functions,
                                    #   variables, etc.) to find its name
                                    for item in c.body:
                                        # the name is defined as an assignment (ast.Assign)
                                        if isinstance(item, ast.Assign):
                                            # does this assignment have a target called 'name'?
                                            for target in item.targets:
                                                if isinstance(target, ast.Name) and target.id == 'name':
                                                    # check if the value assigned to 'name' is a constant
                                                    if isinstance(item.value, ast.Constant):
                                                        pluginNames.append(item.value.value)
                except Exception as e:
                    logging.error(f"{type(e).__name__} identifying NetBox plugin names: {e}")

        if pluginNames:
            pluginNames = list(set(pluginNames))
            # at this point we have a list of plugin names for all of the plugin classes!
            #   we need to make sure they exist in plugins.py

            # Load and parse the plugins.py file
            pluginsListFound = False
            with open(os.path.join(args.netboxConfigDir, 'plugins.py'), 'r') as pluginFile:
                code = pluginFile.read()
                tree = ast.parse(code)

            # Walk the AST to find the PLUGINS assignment
            class PluginListModifier(ast.NodeTransformer):
                def visit_Assign(self, node):
                    global pluginsListFound
                    if isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'PLUGINS':
                        pluginsListFound = True
                        # Check if the node's value is a list
                        if isinstance(node.value, ast.List):
                            # Get the existing plugin names in the list
                            existingPlugins = {elt.s for elt in node.value.elts if isinstance(elt, ast.Str)}
                            # Add new plugins if they aren't already in the list
                            for plugin in pluginNames:
                                if plugin not in existingPlugins:
                                    node.value.elts.append(ast.Constant(value=plugin))
                    return node

            # Modify the AST
            modifier = PluginListModifier()
            modifiedTree = modifier.visit(tree)

            # # If PLUGINS was not found, add it at the end of the module
            if not pluginsListFound:
                modifiedTree.body.append(
                    ast.Assign(
                        targets=[ast.Name(id='PLUGINS', ctx=ast.Store())],
                        value=ast.List(elts=[ast.Constant(value=plugin) for plugin in pluginNames], ctx=ast.Load()),
                    )
                )

            # Unparse the modified AST back into code
            modifiedCode = ast.unparse(ast.fix_missing_locations(modifiedTree))

            # Write the modified code back to the file
            with open(os.path.join(args.netboxConfigDir, 'plugins.py'), 'w') as pluginFile:
                pluginFile.write(modifiedCode)

        # END CUSTOM PLUGIN INSTALLATION #############################################################################

    # if there is a database backup .gz in the preload directory, load it up (preferring the newest)
    # if there are multiple) instead of populating via API
    preloadDatabaseFile = args.preloadBackupFile
    preloadDatabaseSuccess = False
    if (not os.path.isfile(preloadDatabaseFile)) and os.path.isdir(args.preloadDir):
        preloadFiles = [
            x
            for x in list(filter(os.path.isfile, glob.glob(os.path.join(args.preloadDir, '*.gz'))))
            if not x.endswith('.media.tar.gz')
        ]
        preloadFiles.sort(key=lambda x: os.path.getmtime(x))
        preloadDatabaseFile = next(iter(preloadFiles), '')

    if os.path.isfile(preloadDatabaseFile):
        # we're loading an existing database directly with postgreSQL
        # this should pretty much match what is in control.py:netboxRestore
        try:
            osEnv = os.environ.copy()
            osEnv['PGPASSWORD'] = args.postgresPassword

            # stop the netbox processes (besides this one)
            cmd = [
                'bash',
                '-c',
                "supervisorctl status netbox:* | grep -v :initialization | awk '{ print $1 }' | xargs -r -L 1 -P 4 supervisorctl stop",
            ]
            err, results = malcolm_utils.run_process(cmd, logger=logging)
            if err != 0:
                logging.error(f'{err} stopping netbox:*: {results}')

            # drop the existing netbox database
            cmd = [
                'dropdb',
                '-h',
                args.postgresHost,
                '-U',
                args.postgresUser,
                '-f',
                args.postgresDB,
            ]
            err, results = malcolm_utils.run_process(cmd, env=osEnv, logger=logging)
            if err != 0:
                logging.warning(f'{err} dropping NetBox database: {results}')

            # create a new netbox database
            cmd = [
                'createdb',
                '-h',
                args.postgresHost,
                '-U',
                args.postgresUser,
                args.postgresDB,
            ]
            err, results = malcolm_utils.run_process(cmd, env=osEnv, logger=logging)
            if err != 0:
                raise Exception(f'Error {err} creating new NetBox database: {results}')

            # make sure permissions are set up right
            cmd = [
                'psql',
                '-h',
                args.postgresHost,
                '-U',
                args.postgresUser,
                '-c',
                f'GRANT ALL PRIVILEGES ON DATABASE {args.postgresDB} TO {args.postgresUser}',
            ]
            err, results = malcolm_utils.run_process(cmd, env=osEnv, logger=logging)
            if err != 0:
                logging.error(f'{err} setting NetBox database permissions: {results}')

            # load the backed-up psql dump
            cmd = [
                'psql',
                '-h',
                args.postgresHost,
                '-U',
                args.postgresUser,
            ]
            with (
                gzip.open(preloadDatabaseFile, 'rt')
                if 'application/gzip' in magic.from_file(preloadDatabaseFile, mime=True)
                else open(preloadDatabaseFile, 'r')
            ) as f:
                err, results = malcolm_utils.run_process(cmd, env=osEnv, logger=logging, stdin=f.read())
            if (err == 0) and results:
                preloadDatabaseSuccess = True
            else:
                raise Exception(f'Error {err} loading NetBox database: {results}')

            # with idaholab/Malcolm#280 we switched to use prefix.description instead of VRF for identifying subnets in NetBox,
            # this will migrate ipam_vrf.name to ipam_prefix.description if we're coming from an older backup
            cmd = [
                'psql',
                '-h',
                args.postgresHost,
                '-U',
                {args.postgresUser},
                '-c',
                "UPDATE ipam_prefix SET description = (SELECT name from ipam_vrf WHERE id = ipam_prefix.vrf_id) WHERE ((description = '') IS NOT FALSE) AND (vrf_id > 0)",
            ]
            err, results = malcolm_utils.run_process(cmd, env=osEnv, logger=logging)
            if err != 0:
                logging.error(f'{err} migrating ipam_vrf.name to ipam_prefix.description: {results}')

            # don't restore users_user, tokens, etc: they're created by Malcolm and may not be the same on this instance
            cmd = [
                'psql',
                '-h',
                args.postgresHost,
                '-U',
                {args.postgresUser},
                '-c',
                'TRUNCATE users_user CASCADE',
            ]
            err, results = malcolm_utils.run_process(cmd, env=osEnv, logger=logging)
            if err != 0:
                logging.error(f'{err} truncating table users_user table: {results}')

            # start back up the netbox processes (except initialization)
            cmd = [
                'bash',
                '-c',
                "supervisorctl status netbox:* | grep -v :initialization | awk '{ print $1 }' | xargs -r -L 1 -P 4 supervisorctl start",
            ]
            err, results = malcolm_utils.run_process(cmd, logger=logging)
            if err != 0:
                logging.error(f'{err} starting netbox:*: {results}')

            with malcolm_utils.pushd(os.path.dirname(manageScript)):
                # migrations if needed
                cmd = [
                    netboxVenvPy,
                    os.path.basename(manageScript),
                    "migrate",
                    "--check",
                ]
                err, results = malcolm_utils.run_process(cmd, logger=logging)
                if err != 0:
                    for operation in [
                        [
                            "migrate",
                            "--no-input",
                        ],
                        [
                            "trace_paths",
                            "--no-input",
                        ],
                        [
                            "remove_stale_contenttypes",
                            "--no-input",
                        ],
                        [
                            "clearsessions",
                        ],
                        [
                            "reindex",
                            "--lazy",
                        ],
                    ]:

                        cmd = [netboxVenvPy, os.path.basename(manageScript)] + operation
                        err, results = malcolm_utils.run_process(cmd, logger=logging)
                        if (err != 0) or (not results):
                            logging.error(f'{err} performing NetBox {cmd[2]}: {results}')

                # create users_user for superuser
                cmd = [
                    netboxVenvPy,
                    os.path.basename(manageScript),
                    "shell",
                    "--interface",
                    "python",
                ]
                with open('/usr/local/bin/netbox_superuser_create.py', 'r') as f:
                    err, results = malcolm_utils.run_process(cmd, logger=logging, stdin=f.read())
                if (err != 0) or (not results):
                    logging.error(f'{err} setting up superuser: {results}')

            # restore media directory
            preloadDatabaseFileParts = os.path.splitext(preloadDatabaseFile)
            mediaFileName = preloadDatabaseFileParts[0] + ".media.tar.gz"
            mediaPath = os.path.join(args.netboxDir, os.path.join('netbox', 'media'))
            if os.path.isfile(mediaFileName) and os.path.isdir(mediaPath):
                try:
                    malcolm_utils.RemoveEmptyFolders(mediaPath, removeRoot=False)
                    with tarfile.open(mediaFileName) as t:
                        t.extractall(mediaPath)
                except Exception as e:
                    logging.error(f"{type(e).__name__} processing restoring {os.path.basename(mediaFileName)}: {e}")

        except Exception as e:
            logging.error(f"{type(e).__name__} restoring {os.path.basename(preloadDatabaseFile)}: {e}")

    # only proceed to do the regular population if if we didn't preload a database backup, or
    #   if we attempted (and failed) but they didn't explicitly specify a backup file
    if not preloadDatabaseSuccess and (not args.preloadBackupFile):
        # create connection to netbox API
        nb = pynetbox.api(
            args.netboxUrl,
            token=args.netboxToken,
            threading=True,
        )
        sites = {}
        groups = {}
        permissions = {}
        prefixes = {}
        devices = {}
        interfaces = {}
        ipAddresses = {}
        deviceTypes = {}
        roles = {}
        manufacturers = {}
        randColor = randomcolor.RandomColor(seed=datetime.now().timestamp())

        # wait for a good connection
        while args.wait:
            try:
                [x.name for x in nb.dcim.sites.all()]
                break
            except Exception as e:
                logging.info(f"{type(e).__name__}: {e}")
                logging.debug("retrying in a few seconds...")
                time.sleep(5)

        # GROUPS #####################################################################################################
        DEFAULT_GROUP_NAMES = (
            args.staffGroupName,
            args.defaultGroupName,
        )

        try:
            groupsPreExisting = {x.name: x for x in nb.users.groups.all()}
            logging.debug(f"groups (before): { {k:v.id for k, v in groupsPreExisting.items()} }")

            # create groups that don't already exist
            for groupName in [x for x in DEFAULT_GROUP_NAMES if x not in groupsPreExisting]:
                try:
                    nb.users.groups.create({'name': groupName})
                except pynetbox.RequestError as nbe:
                    logging.warning(f"{type(nbe).__name__} processing group \"{groupName}\": {nbe}")

            groups = {x.name: x for x in nb.users.groups.all()}
            logging.debug(f"groups (after): { {k:v.id for k, v in groups.items()} }")
        except Exception as e:
            logging.error(f"{type(e).__name__} processing groups: {e}")

        # PERMISSIONS ##################################################################################################
        DEFAULT_PERMISSIONS = {
            f'{args.staffGroupName}_permission': {
                'name': f'{args.staffGroupName}_permission',
                'enabled': True,
                'groups': [args.staffGroupName],
                'actions': [
                    'view',
                    'add',
                    'change',
                    'delete',
                ],
                'exclude_objects': [],
            },
            f'{args.defaultGroupName}_permission': {
                'name': f'{args.defaultGroupName}_permission',
                'enabled': True,
                'groups': [args.defaultGroupName],
                'actions': [
                    'view',
                    'add',
                    'change',
                    'delete',
                ],
                'exclude_objects': [
                    'admin.logentry',
                    'auth.group',
                    'auth.permission',
                    'auth.user',
                    'users.admingroup',
                    'users.adminuser',
                    'users.objectpermission',
                    'users.token',
                    'users.userconfig',
                ],
            },
        }

        try:
            # get all content types (for creating new permissions)
            allObjectTypeNames = [f'{x.app_label}.{x.model}' for x in nb.extras.object_types.all()]

            permsPreExisting = {x.name: x for x in nb.users.permissions.all()}
            logging.debug(f"permissions (before): { {k:v.id for k, v in permsPreExisting.items()} }")

            # create permissions that don't already exist
            for permName, permConfig in {
                k: v
                for (k, v) in DEFAULT_PERMISSIONS.items()
                if v.get('name', None) and v['name'] not in permsPreExisting
            }.items():
                permConfig['groups'] = [groups[x].id for x in permConfig['groups']]
                permConfig['object_types'] = [
                    ct for ct in allObjectTypeNames if ct not in permConfig['exclude_objects']
                ]
                permConfig.pop('exclude_objects', None)
                try:
                    nb.users.permissions.create(permConfig)
                except pynetbox.RequestError as nbe:
                    logging.warning(f"{type(nbe).__name__} processing permission \"{permConfig['name']}\": {nbe}")

            permissions = {x.name: x for x in nb.users.permissions.all()}
            logging.debug(f"permissions (after): { {k:v.id for k, v in permissions.items()} }")
        except Exception as e:
            logging.error(f"{type(e).__name__} processing permissions: {e}")

        # ###### MANUFACTURERS #########################################################################################
        try:
            manufacturersPreExisting = {x.name: x for x in nb.dcim.manufacturers.all()}
            logging.debug(f"Manufacturers (before): { {k:v.id for k, v in manufacturersPreExisting.items()} }")

            # create manufacturers that don't already exist
            for manufacturerName in [x for x in args.manufacturers if x not in manufacturersPreExisting]:
                try:
                    nb.dcim.manufacturers.create(
                        {
                            "name": manufacturerName,
                            "slug": slugify(manufacturerName),
                        },
                    )
                except pynetbox.RequestError as nbe:
                    logging.warning(f"{type(nbe).__name__} processing manufacturer \"{manufacturerName}\": {nbe}")

            manufacturers = {x.name: x for x in nb.dcim.manufacturers.all()}
            logging.debug(f"Manufacturers (after): { {k:v.id for k, v in manufacturers.items()} }")
        except Exception as e:
            logging.error(f"{type(e).__name__} processing manufacturers: {e}")

        # ###### ROLES #################################################################################################
        try:
            rolesPreExisting = {x.name: x for x in nb.dcim.device_roles.all()}
            logging.debug(f"Roles (before): { {k:v.id for k, v in rolesPreExisting.items()} }")

            # create roles that don't already exist
            for roleName in [x for x in args.roles if x not in rolesPreExisting]:
                try:
                    nb.dcim.device_roles.create(
                        {
                            "name": roleName,
                            "slug": slugify(roleName),
                            "vm_role": True,
                            "color": randColor.generate()[0][1:],
                        },
                    )
                except pynetbox.RequestError as nbe:
                    logging.warning(f"{type(nbe).__name__} processing role \"{roleName}\": {nbe}")

            roles = {x.name: x for x in nb.dcim.device_roles.all()}
            logging.debug(f"Roles (after): { {k:v.id for k, v in roles.items()} }")
        except Exception as e:
            logging.error(f"{type(e).__name__} processing roles: {e}")

        # ###### DEVICE TYPES ##########################################################################################
        try:
            deviceTypesPreExisting = {x.model: x for x in nb.dcim.device_types.all()}
            logging.debug(f"Device types (before): { {k:v.id for k, v in deviceTypesPreExisting.items()} }")

            # create device types that don't already exist
            for deviceTypeModel in [x for x in args.deviceTypes if x not in deviceTypesPreExisting]:
                try:
                    manuf = min_hash_value_by_value(manufacturers)
                    nb.dcim.device_types.create(
                        {
                            "model": deviceTypeModel,
                            "slug": slugify(deviceTypeModel),
                            "manufacturer": manuf.id if manuf else None,
                        },
                    )
                except pynetbox.RequestError as nbe:
                    logging.warning(f"{type(nbe).__name__} processing device type \"{deviceTypeModel}\": {nbe}")

            deviceTypes = {x.model: x for x in nb.dcim.device_types.all()}
            logging.debug(f"Device types (after): { {k:v.id for k, v in deviceTypes.items()} }")
        except Exception as e:
            logging.error(f"{type(e).__name__} processing device types: {e}")

        # ###### SITES #################################################################################################
        try:
            sitesPreExisting = {x.name: x for x in nb.dcim.sites.all()}
            logging.debug(f"sites (before): { {k:v.id for k, v in sitesPreExisting.items()} }")

            # create sites that don't already exist
            for siteName in [x for x in args.netboxSites if x not in sitesPreExisting]:
                try:
                    nb.dcim.sites.create(
                        {
                            "name": siteName,
                            "slug": slugify(siteName),
                        },
                    )
                except pynetbox.RequestError as nbe:
                    logging.warning(f"{type(nbe).__name__} processing site \"{siteName}\": {nbe}")

            sites = {x.name: x for x in nb.dcim.sites.all()}
            logging.debug(f"sites (after): { {k:v.id for k, v in sites.items()} }")
        except Exception as e:
            logging.error(f"{type(e).__name__} processing sites: {e}")

        # ###### Missing prefix descriptions from VRF names (see idaholab/Malcolm#280) ##################################
        try:
            for prefix in [x for x in nb.ipam.prefixes.filter(description__empty=True) if x.vrf]:
                logging.debug(f"Updating prefix {str(prefix)}'s description to {str(prefix.vrf)}")
                prefix.update(
                    {
                        "description": str(prefix.vrf),
                    }
                )

        except Exception as e:
            logging.error(f"{type(e).__name__} migrating prefix VRF to prefix description: {e}")

    # ###### Netbox-Initializers ###################################################################################
    # We're doing the netbox initializers even if we did a database backup preload, as we always want to
    #   honor there being something in here. If things already got loaded upas part of the backup preload,
    #   no big deal, those things will just fail for already existing.
    if os.path.isfile(netboxVenvPy) and os.path.isfile(manageScript) and os.path.isdir(args.preloadDir):
        try:
            with malcolm_utils.pushd(os.path.dirname(manageScript)):
                # make a local copy of the YMLs to preload
                with tempfile.TemporaryDirectory() as tmpPreloadDir:
                    copy_tree(args.preloadDir, tmpPreloadDir)
                    retcode, output = malcolm_utils.run_process(
                        [
                            netboxVenvPy,
                            os.path.basename(manageScript),
                            "load_initializer_data",
                            "--path",
                            tmpPreloadDir,
                        ],
                        logger=logging,
                    )
                    if retcode == 0:
                        logging.debug(f"netbox-initializers: {retcode} {output}")
                    else:
                        logging.error(f"Error processing netbox-initializers: {retcode} {output}")

        except Exception as e:
            logging.error(f"{type(e).__name__} processing netbox-initializers: {e}")

    # this if statement is the same check we did above after the preload backup restore finished
    if not preloadDatabaseSuccess and (not args.preloadBackupFile):
        # ######  Device-Type-Library-Import ###########################################################################
        if os.path.isdir(args.libraryDir):
            try:
                with malcolm_utils.pushd(args.libraryDir):
                    osEnv = os.environ.copy()
                    osEnv['NETBOX_URL'] = args.netboxUrl
                    osEnv['NETBOX_TOKEN'] = args.netboxToken
                    osEnv['REPO_URL'] = 'local'
                    cmd = [netboxVenvPy, 'nb-dt-import.py']
                    err, results = malcolm_utils.run_process(
                        cmd,
                        logger=logging,
                        env=osEnv,
                    )
                    if (err != 0) or (not results):
                        logging.error(f"{err} running nb-dt-import.py: {results}")

            except Exception as e:
                logging.error(f"{type(e).__name__} processing library: {e}")


###################################################################################################
if __name__ == '__main__':
    main()
