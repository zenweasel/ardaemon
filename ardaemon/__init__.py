__author__ = 'ardevelop'

import os
import sys
import pwd
import grp
import time
import signal
import fcntl
import threading
import multiprocessing
import argparse

#region constants

ERROR_MESSAGE_PATTERN = "ERROR: %s\n"
WARNING_MESSAGE_PATTERN = "WARNING: %s\n"
START = "start"
STOP = "stop"
RESTART = "restart"
INSTALL = "install"
ACTIONS = [START, STOP, RESTART, INSTALL]
INSTALL_SCRIPT = """
#!/bin/sh
#
# %(name)s: autogenerated by ardaemon on %(generated)s
#
# chkconfig:   - 20 80
# description: Starts and stops daemon.

# Source function library.
. /etc/rc.d/init.d/functions

name="%(name)s"
executable="%(executable)s"
pidfile="%(pidfile)s"

start() {
    $executable -s start
    retval=$?
    rh_status
    return $retval
}

stop() {
    $executable -s stop
    retval=$?
    rh_status
    return $retval
}

restart() {
    stop
    start
}

reload() {
    false
}

rh_status() {
    status -p $pidfile $name
}

rh_status_q() {
    rh_status >/dev/null 2>&1
}


case "$1" in
    start)
        $1
        ;;
    stop)
        $1
        ;;
    restart)
        $1
        ;;
    reload)
        false
        ;;
    status)
        rh_status
        ;;
    condrestart|try-restart)
        rh_status_q || exit 0
        restart
        ;;
    *)
        echo $"Usage: $0 {start|stop|status|restart|condrestart|try-restart}"
        exit 2
esac
exit $?
"""

#endregion

try:
    from setproctitle import setproctitle
except ImportError:
    sys.stderr.write(WARNING_MESSAGE_PATTERN % "No module \"setproctitle\"\n")

    def setproctitle(title):
        pass


class Daemon:
    def __init__(self, name=None, pid_path="/var/run", title=None, user=None, group=None, parser=None, working_dir=None,
                 stdout=os.devnull, stdin=os.devnull, stderr=os.devnull):

        path, executable = os.path.split(os.path.abspath(sys.argv[0]))

        self.name = name = name or os.path.splitext(executable)[0]
        self.pid_path = pid_path = pid_path or path
        self.pid_file = os.path.join(pid_path, "%s.pid" % name)
        self.working_dir = working_dir or path
        self.title = title
        self.user = user
        self.group = group
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.parser = parser
        self.daemon = False

    def __enter__(self):
        parser = self.parser or argparse.ArgumentParser()
        group = parser.add_argument_group('service')
        group.add_argument("-s", metavar="cmd", default=None, choices=ACTIONS, type=str, dest="_cmd", help="command")
        group.add_argument("-sn", metavar="name", default=None, type=str, dest="_name", help="name")
        group.add_argument("-su", metavar="user", default=None, type=str, dest="_user", help="run as user")
        group.add_argument("-sg", metavar="group", default=None, type=str, dest="_group", help="run as group")
        group.add_argument("-sp", metavar="path", default=None, type=str, dest="_path", help="pid file path")
        group.add_argument("-sw", metavar="path", default=None, type=str, dest="_wd", help="working directory")
        group.add_argument("-st", metavar="title", default=None, type=str, dest="_title", help="process title")
        group.add_argument("-si", metavar="python", default=None, type=str, dest="_python", help="python interpreter")
        group.add_argument("-stdout", metavar="path", default=None, type=str, dest="_stdout", help="output stream")
        group.add_argument("-stdin", metavar="path", default=None, type=str, dest="_stdin", help="input stream")
        group.add_argument("-stderr", metavar="path", default=None, type=str, dest="_stderr", help="error stream")

        self.args = args = parser.parse_args()
        self.name = args._name or self.name
        self.user = args._user or self.user
        self.group = args._group or self.group
        self.title = args._title or self.title
        self.working_dir = args._wd or self.working_dir
        self.stdout = args._stdout or self.stdout
        self.stdin = args._stdin or self.stdin
        self.stderr = args._stderr or self.stderr
        self.pid_file = os.path.join(args._path or self.pid_path, "%s.pid" % self.name)

        command = self.args._cmd
        if START == command:
            self.start()
        elif STOP == command:
            self.stop()
            sys.exit(0)
        elif RESTART == command:
            self.stop()
            self.start()
        elif INSTALL == command:
            self.install()
            sys.exit(0)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.daemon and self.daemon_process == os.getpid():
            self.pf_del()

    def error(self, msg):
        sys.stderr.write(ERROR_MESSAGE_PATTERN % msg)
        sys.exit(1)

    def pf_del(self):
        try:
            os.remove(self.pid_file)
        except OSError:
            pass

    def pf_get(self):
        try:
            with open(self.pid_file, "r") as fp:
                return int(fp.read().strip())
        except (IOError, ValueError):
            return None

    def pf_set(self):
        try:
            pid = os.getpid()
            with open(self.pid_file, "w+") as fp:
                fp.write(str(pid))
        except (IOError, OSError), ex:
            self.error("Cannot create pid file to \"%s\" with error \"%s\"." % (self.pid_file, ex))

    def pf_init(self, uid, gid):
        try:
            with open(self.pid_file, "w+"):
                pass
            os.chmod(self.pid_file, 0660)
            os.chown(self.pid_file, uid, gid)

        except (IOError, OSError), ex:
            self.error("Cannot init pid file to \"%s\" with error \"%s\"." % (self.pid_file, ex))

    def demonize(self):
        try:
            if self.user:
                user = pwd.getpwnam(self.user)
            else:
                user = pwd.getpwuid(os.getuid())
        except KeyError:
            return self.error("User \"%s\" not found." % self.user)

        try:
            gid = grp.getgrnam(self.group).gr_gid if self.group else user.pw_gid
        except KeyError:
            return self.error(ERROR_MESSAGE_PATTERN % ("Group \"%s\" not found." % self.group))

        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError:
            return self.error("Error occurred on fork #1.")

        self.pf_init(user.pw_uid, gid)

        os.setgid(gid)
        os.setuid(user.pw_uid)
        os.chdir(self.working_dir)
        os.setsid()
        os.umask(0)

        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError:
            return self.error("Error occurred on fork #2.")

        self.pf_set()

        if self.title:
            setproctitle(self.title)

        sys.stdin = file(self.stdin, 'r') if isinstance(self.stdin, str) else self.stdin
        sys.stdout = file(self.stdout, 'w+') if isinstance(self.stdout, str) else self.stdout
        sys.stderr = file(self.stderr, 'w+', 0) if isinstance(self.stderr, str) else self.stderr

    def start(self):
        pid = self.pf_get()

        if pid:
            return self.error("Daemon is already running.")

        self.daemon = True
        self.daemon_process = os.getpid()
        self.demonize()

    def stop(self):
        pid = self.pf_get()

        if pid:
            try:
                while 1:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(0.1)
            except OSError, ex:
                if str(ex).find("No such process") > 0:
                    self.pf_del()
                else:
                    return self.error("Error on stopping server with message \"%s\"." % ex)
        else:
            return self.error("Daemon pid file not found.")

    def install(self):
        import platform

        system = platform.system()

        if "Linux" == system:
            self.install_for_linux()
        else:
            self.error("Not implemented install script for system \"%s\"" % system)

    def install_for_linux(self):
        import datetime

        executable = self.args._python or "python"

        argv_iter = iter(sys.argv)
        try:
            while 1:
                val = argv_iter.next()
                if val in ("-s", "-si"):
                    argv_iter.next()
                else:
                    executable += " " + val
        except StopIteration:
            pass

        script = INSTALL_SCRIPT % {
            "name": self.name,
            "pidfile": self.pid_file,
            "executable": executable,
            "generated": datetime.datetime.now()
        }

        script_path = "/etc/rc.d/init.d/%s" % self.name
        if os.path.exists(script_path):
            self.error("Daemon already installed.")
        else:
            try:
                with open(script_path, "w+") as fp:
                    fp.write(script)
                os.chmod(script_path, 0755)

                print "Successfully install."
            except (IOError, OSError), ex:
                self.error("Installation Error. %s." % ex)


def add_watch_thread(parent_process_id, frequency=0.1):
    def _watch_thread_job(pid):
        while True:
            try:
                os.kill(pid, 0)
                time.sleep(frequency)
            except OSError:
                os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_watch_thread_job, args=(parent_process_id,)).start()


def subprocess(target, title=None, args=None, kwargs=None):
    parent_pid = os.getpid()

    def child(parent_pid, title, target, args, kwargs):
        if title:
            setproctitle(title)

        add_watch_thread(parent_pid)
        target(*(args or ()), **(kwargs or {}))

    process = multiprocessing.Process(target=child, args=(parent_pid, title, target, args, kwargs))
    process.start()

    return process


def subprocess_module(module_name, method_name, title=None, args=None, kwargs=None):
    def target(*args, **kwargs):
        module = __import__(module_name)

        module_path = module_name.split('.')
        if len(module_path) > 1:
            module_path = module_path[1:]
            for module_part in module_path:
                module = getattr(module, module_part)

        getattr(module, method_name)(*args, **kwargs)

    subprocess(target, title, args, kwargs)


def get_process_id():
    return os.getpid()


def set_title(title):
    setproctitle(title)


def infinite_loop():
    while True:
        time.sleep(1)


if "__main__" == __name__:
    if len(sys.argv) > 1:
        with Daemon():
            pass
    else:
        executable = sys.argv[0]
        os.system("python %s -h" % executable)