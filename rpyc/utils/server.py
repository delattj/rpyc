"""
rpyc plug-in server (threaded or forking)
"""
import sys
import os
import socket
import time
import threading
import errno
import logging
from rpyc.core import SocketStream, Channel, Connection
from rpyc.utils.registry import UDPRegistryClient
from rpyc.utils.authenticators import AuthenticationError
from rpyc.lib import safe_import
signal = safe_import("signal")


class Server(object):
    def __init__(self, service, hostname = "0.0.0.0", port = 0, backlog = 10, 
            reuse_addr = True, authenticator = None, registrar = None, 
            auto_register = True, protocol_config = {}, logger = None):
        self.active = False
        self._closed = False
        self.service = service
        self.authenticator = authenticator
        self.backlog = backlog
        self.auto_register = auto_register
        self.protocol_config = protocol_config
        self.clients = set()
        
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if reuse_addr and sys.platform != "win32":
            # warning: reuseaddr is not what you expect on windows!
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        self.listener.bind((hostname, port))
        self.port = self.listener.getsockname()[1]
        
        if logger is None:
            logger = self._get_logger()
        self.logger = logger
        if registrar is None:
            registrar = UDPRegistryClient(logger = self.logger)
        self.registrar = registrar
    
    def _get_logger(self):
        return logging.getLogger("%s/%d" % (self.service.get_service_name(), self.port))
    
    def close(self):
        if self._closed:
            return
        self._closed = True
        self.active = False
        if self.auto_register:
            try:
                self.registrar.unregister(self.port)
            except Exception:
                 self.logger.exception("error unregistering services")
        self.listener.close()
        self.logger.info("listener closed")
        for c in set(self.clients):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            c.close()
        self.clients.clear()
    
    def fileno(self):
        return self.listener.fileno()
    
    def accept(self):
        while True:
            try:
                sock, (h, p) = self.listener.accept()
            except socket.timeout:
                pass
            except socket.error:
                ex = sys.exc_info()[1]
                if ex[0] == errno.EINTR:
                    pass
                else:
                    raise EOFError()
            else:
                break
        
        sock.setblocking(True)
        self.logger.info("accepted %s:%s", h, p)
        self.clients.add(sock)
        self._accept_method(sock)
    
    def _accept_method(self, sock):
        """this method should start a thread, fork a child process, or 
        anything else in order to serve the client. once the mechanism has
        been created, it should invoke _authenticate_and_serve_client with 
        `sock` as the argument"""
        raise NotImplementedError

    def _authenticate_and_serve_client(self, sock):
        try:
            if self.authenticator:
                h, p = sock.getpeername()
                try:
                    sock, credentials = self.authenticator(sock)
                except AuthenticationError:
                    self.logger.info("%s:%s failed to authenticate, rejecting connection", h, p)
                    return
                else:
                    self.logger.info("%s:%s authenticated successfully", h, p)
            else:
                credentials = None
            try:
                self._serve_client(sock, credentials)
            except Exception:
                self.logger.exception("client connection terminated abruptly")
                raise
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            sock.close()
            self.clients.discard(sock)
    
    def _serve_client(self, sock, credentials):
        h, p = sock.getpeername()
        if credentials:
            self.logger.info("welcome %s:%s (%r)", h, p, credentials)
        else:
            self.logger.info("welcome %s:%s", h, p)
        try:
            config = dict(self.protocol_config, credentials = credentials)
            conn = Connection(self.service, Channel(SocketStream(sock)), 
                config = config, _lazy = True)
            conn._init_service()
            conn.serve_all()
        finally:
            self.logger.info("goodbye %s:%s", h, p)
    
    def _bg_register(self):
        interval = self.registrar.REREGISTER_INTERVAL
        self.logger.info("started background auto-register thread "
            "(interval = %s)", interval)
        tnext = 0
        try:
            while self.active:
                t = time.time()
                if t >= tnext: 
                    tnext = t + interval
                    try:
                        self.registrar.register(self.service.get_service_aliases(),
                            self.port)
                    except Exception:
                        self.logger.exception("error registering services")
                time.sleep(1)
        finally:
            if not self._closed:
                self.logger.info("background auto-register thread finished")
    
    def start(self):
        """starts the server. use close() to stop"""
        self.listener.listen(self.backlog)
        h, p = self.listener.getsockname()
        self.logger.info("server started on %s:%s", h, p)
        self.active = True
        if self.auto_register:
            t = threading.Thread(target = self._bg_register)
            t.setDaemon(True)
            t.start()
        #if sys.platform == "win32":
        # hack so we can receive Ctrl+C on windows
        self.listener.settimeout(0.5)
        try:
            try:
                while True:
                    self.accept()
            except EOFError:
                pass # server closed by another thread
            except KeyboardInterrupt:
                print("")
                self.logger.warn("keyboard interrupt!")
        finally:
            self.logger.info("server has terminated")
            self.close()


class ThreadedServer(Server):
    def _accept_method(self, sock):
        t = threading.Thread(target = self._authenticate_and_serve_client, args = (sock,))
        t.setDaemon(True)
        t.start()


class ForkingServer(Server):
    def __init__(self, *args, **kwargs):
        if not signal:
            raise OSError("ForkingServer not supported on this platform")
        Server.__init__(self, *args, **kwargs)
        # setup sigchld handler
        self._prevhandler = signal.signal(signal.SIGCHLD, self._handle_sigchld)
    
    def close(self):
        Server.close(self)
        signal.signal(signal.SIGCHLD, self._prevhandler)
       
    @classmethod
    def _handle_sigchld(cls, signum, unused):
        try:
            while True:
                pid, dummy = os.waitpid(-1, os.WNOHANG)
                if pid <= 0:
                    break
        except OSError:
            pass
        # re-register signal handler (see man signal(2), under Portability)
        signal.signal(signal.SIGCHLD, cls._handle_sigchld)
    
    def _accept_method(self, sock):
        pid = os.fork()
        if pid == 0:
            # child
            try:
                try:
                    self.logger.debug("child process created")
                    signal.signal(signal.SIGCHLD, self._prevhandler)
                    self.listener.close()
                    self.clients.clear()
                    self._authenticate_and_serve_client(sock)
                except:
                    self.logger.exception("child process terminated abnormally")
                else:
                    self.logger.debug("child process terminated")
            finally:
                self.logger.debug("child terminated")
                os._exit(0)
        else:
            # parent
            sock.close()
