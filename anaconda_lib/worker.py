# -*- coding: utf8 -*-

# Copyright (C) 2013 - Oscar Campos <oscar.campos@member.fsf.org>
# This program is Free Software see LICENSE file for details

import os
import sys
import time
import errno
import socket
import logging
import threading

import sublime

from .jsonclient import AsynClient
from .vagrant import VagrantStatus
from .helpers import (
    get_settings, get_traceback, project_name, create_subprocess, active_view
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler(sys.stdout))
logger.setLevel(logging.WARNING)

WORKERS = {}
WORKERS_LOCK = threading.RLock()
LOOP_RUNNING = False


class BaseWorker(object):
    """Base class for different worker interfaces
    """

    def __init__(self):
        self.hostname = 'localhost'
        self.available_port = None
        self.reconnecting = False
        self.last_error = None
        self.process = None
        self.client = None

    @property
    def port(self):
        """This method hast to be reimplementted
        """

        raise RuntimeError('This method must be reimplemented')

    def start_json_server(self):
        """Starts the JSON server
        """

        if self.server_is_active():
            if self.server_is_healthy():
                return

            self.sanitize_server()
            return

        logger.info('Starting anaconda JSON server...')
        self.build_server()

    def server_is_active(self):
        """Checks if the server is already active
        """

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((self.hostname, self.available_port))
            s.close()
        except socket.error as error:
            if error.errno == errno.ECONNREFUSED:
                return False
            else:
                logger.error(
                    'Unexpected error in `server_is_active`: {}'.format(error)
                )
                return False
        else:
            return True

    def server_is_healthy(self):
        """Checks if the server process is healthy
        """

        if self.process.poll() is None:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect((self.hostname, self.available_port))
                s.sendall(bytes('{"method": "check"}', 'utf8'))
                data = sublime.value_decode(s.recv(1024))
                s.close()
            except:
                return False

            return data == b'Ok'
        else:
            logger.error(
                'Something is using the port {} in your system'.format(
                    self.available_port
                )
            )
            return False

    def _execute(self, callback, **data):
        """This method has to be reimplemented
        """

        raise RuntimeError('This method has to be reimplemented')


class LocalWorker(BaseWorker):
    """This worker is used with local interpreter
    """

    @property
    def port(self):
        """Get the first available port
        """

        s = socket.socket()
        s.bind(('', 0))
        port = s.getsockname()[1]
        s.close()

        return port

    def start(self):
        """Start this LocalWorker
        """

        if not self.available_port:
            self.available_port = self.port

        try:
            if self.reconnecting is True:
                self.available_port = self.port

            self.start_json_server()
            while not self.server_is_active():
                time.sleep(0.01)

            self.client = AsynClient(self.available_port)
        except Exception as error:
            logging.error(error)
            logging.error(get_traceback())

    def sanitize_server(self):
        """Disconnect all the clients and terminate the server process
        """

        self.client.close()
        self.process.kill()
        self.start_json_server()

    def build_server(self):
        """Create the subprocess for the anaconda json server
        """

        script_file = os.path.join(
            os.path.dirname(__file__),
            '../anaconda_server{}jsonserver.py'.format(os.sep)
        )

        view = sublime.active_window().active_view()
        paths = get_settings(view, 'extra_paths', [])
        try:
            paths.extend(sublime.active_window().folders())
        except AttributeError:
            sublime.error_message(
                'Your `extra_paths` configuration is a string but we are '
                'expecting a list of strings.'
            )
            paths = paths.split(',')
            paths.extend(sublime.active_window().folders())

        try:
            view = sublime.active_window().active_view()
            python = get_settings(view, 'python_interpreter', 'python')
            python = os.path.expanduser(python)
        except:
            python = 'python'

        args = [
            python, '-B', script_file,  '-p',
            project_name(), str(self.available_port)
        ]
        if paths:
            args.extend(['-e', ','.join(paths)])

        args.extend([str(os.getpid())])
        self.process = create_subprocess(args)

    def _execute(self, callback, **data):
        """Execute the given method in the remote server
        """

        if self.client is not None:
            if not self.client.connected:
                self.reconnecting = True
                self.start()

            self.client.send_command(callback, **data)


class RemoteWorker(BaseWorker):
    """This worker is used with non local machine interpreters
    """

    def __init__(self):
        super(RemoteWorker, self).__init__()
        self.config = active_view().settings().get('vagrant_environment')
        self.check_config()
        self.hostname = self.config['network'].get('address')
        self.available_port = self.port
        self.check_status()
        self.support = True

    @property
    def port(self):
        """Return the right port for the given vagrant configuration
        """

        return self.config['network'].get('port', 19360)

    def start(self):
        """Start the jsonserver in the remote guest machine
        """

        if self.support is False:
            sublime.error_message(
                'Anaconda: vagrant support seems to be deactivated, that '
                'means that there were some problem with the configuration '
                'or maybe previous attempts to start a vagrant environemnt '
                'just failed. Did you forget to run command palette '
                '\'Anaconda: Vagrant activate\' after fix some problem?'
            )
            self.support = False
            return

        if not os.path.exists(os.path.expanduser(self.config['directory'])):
            sublime.error_message(
                '{} does not exists!'.format(self.config['directory'])
            )
        else:
            while not self.server_is_active():
                if self.support is not True:
                    return
                time.sleep(0.01)

            self.client = AsynClient(self.available_port)

    def check_config(self):
        """Check the vagrant project configuration
        """

        success = True
        if not self.config.get('directory') or not self.config.get('network'):
            success = False

        if self.config['network'].get('mode') is None:
            success = False
        if self.config['network'].get('port') is None:
            success = False

        if success is False:
            sublime.error_message(
                'Anaconda has detected that your vagrant_environment config '
                'is not valid or complete. Please, refer to https://'
                'github.com/DamnWidget/anaconda/wiki/Vagrant-Environments\n\n'
                'You may need to execute command palette \'Anaconda: '
                'Vagrant activate\' to re-activate anaconda  vagrant support'
            )
            self.support = False

        return success

    def check_status(self):
        """Check vagrant status
        """

        checked = False

        def status(result):
            success, out, error = result
            if not success:
                logging.error('Anaconda: {}'.format(error.decode('utf8')))
                self.support = False
                checked = True
            else:
                checked = True
                assert checked

        VagrantStatus(status, self.config['directory'], self.config['machine'])
        while not checked:
            time.sleep(0.01)


class Worker(object):
    """Worker class that start the server and handle the function calls
    """

    _shared_state = {}

    def __init__(self):
        self.__dict__ = Worker._shared_state

    def vagrant_is_active(self):
        """Determines if vagrant is active for this project
        """

        return active_view().settings().get('vagrant_environment') is not None

    def execute(self, callback, **data):
        """Execute the given method remotely and call the callback with result
        """

        window_id = sublime.active_window().id()
        with WORKERS_LOCK:
            if not window_id in WORKERS:
                try:
                    if self.vagrant_is_active():
                        WORKERS[window_id] = RemoteWorker()
                    else:
                        WORKERS[window_id] = LocalWorker()
                except Exception as error:
                    logging.error(error)
                    logging.error(get_traceback())

        worker = WORKERS[window_id]
        if worker.client is not None:
            print(worker.client)
            if not worker.client.connected:
                worker.reconnecting = True
                worker.start()
            else:
                worker.client.send_command(callback, **data)
        else:
            worker.start()