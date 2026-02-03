import socket
import select
import threading
import logging
import time

class TcpProxy:
    def __init__(self, target_host, target_port=4403, listen_host='0.0.0.0', listen_port=4403):
        self.target_host = target_host
        self.target_port = int(target_port)
        self.listen_host = listen_host
        self.listen_port = int(listen_port)
        self.server_socket = None
        self.target_socket = None
        self.clients = []
        self.running = False
        self.init_buffer = b''
        self.init_buffer_done = False
        self.buffer_time = 5.0  # seconds to buffer startup data (increased for safety)

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
        if self.target_socket:
            try:
                self.target_socket.close()
            except:
                pass

    def _run(self):
        logging.info(f"Starting TCP Proxy on {self.listen_host}:{self.listen_port} -> {self.target_host}:{self.target_port}")

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server_socket.bind((self.listen_host, self.listen_port))
        except Exception as e:
            logging.error(f"Failed to bind proxy port {self.listen_port}: {e}")
            self.running = False
            return
            
        self.server_socket.listen(5)

        # Connect to target
        backoff = 1
        while self.running:
            try:
                self.target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.target_socket.connect((self.target_host, self.target_port))
                logging.info(f"Proxy connected to target device at {self.target_host}:{self.target_port}")
                break
            except Exception as e:
                logging.error(f"Failed to connect to target ({self.target_host}): {e}. Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

        if not self.running:
            return

        inputs = [self.server_socket, self.target_socket]
        start_time = time.time()
        last_target_activity = time.time()
        watchdog_timeout = 60.0  # Reconnect if no data from target for 60s
        last_heartbeat_log = time.time()

        while self.running:
            try:
                # Filter out closed sockets from inputs
                current_inputs = [s for s in inputs + self.clients if s.fileno() != -1]
                readable, _, _ = select.select(current_inputs, [], [], 1.0)
            except Exception as e:
                logging.error(f"Select error: {e}")
                # Clean up closed sockets from our list
                self.clients = [c for c in self.clients if c.fileno() != -1]
                continue

            current_time = time.time()

            # Heartbeat Logging & Watchdog Check
            if current_time - last_heartbeat_log > 60.0:
                silence_duration = current_time - last_target_activity
                logging.info(f"Proxy Heartbeat: Connected. Last data from radio {silence_duration:.1f}s ago. Clients: {len(self.clients)}")
                last_heartbeat_log = current_time
            
            # Watchdog: Force reconnect if silence is too long
            if current_time - last_target_activity > watchdog_timeout:
                logging.warning(f"Watchdog: No data from radio for {watchdog_timeout}s. Forcing reconnect...")
                try:
                    self.target_socket.close()
                except:
                    pass
                
                # Reconnect logic
                reconnected = False
                backoff = 1
                while self.running and not reconnected:
                    try:
                        self.target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        self.target_socket.connect((self.target_host, self.target_port))
                        logging.info("Watchdog: Reconnected to target successfully.")
                        last_target_activity = time.time() # Reset timer
                        reconnected = True
                    except Exception as ex:
                        logging.error(f"Watchdog reconnect failed: {ex}. Retrying in {backoff}s...")
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 10)

            # Check for init buffer timeout
            if not self.init_buffer_done and (current_time - start_time > self.buffer_time):
                self.init_buffer_done = True
                if self.init_buffer:
                    logging.info(f"Init buffer capture finished. Size: {len(self.init_buffer)} bytes")

            for sock in readable:
                if sock is self.server_socket:
                    try:
                        client_socket, addr = self.server_socket.accept()
                        logging.info(f"New proxy connection from {addr}")
                        self.clients.append(client_socket)
                        # Replay init buffer
                        if self.init_buffer:
                            try:
                                client_socket.sendall(self.init_buffer)
                                logging.info(f"Sent {len(self.init_buffer)} bytes of cached init data to {addr}")
                            except Exception as e:
                                logging.error(f"Error sending init buffer to client: {e}")
                    except Exception as e:
                         logging.error(f"Error accepting connection: {e}")

                elif sock is self.target_socket:
                    last_target_activity = time.time() # Update activity timestamp
                    try:
                        data = self.target_socket.recv(4096)
                        if not data:
                            logging.warning("Target closed connection. Restarting proxy connection...")
                            # Close the target socket
                            self.target_socket.close()
                            
                            # Attempt to reconnect loop
                            reconnected = False
                            backoff = 1
                            while self.running and not reconnected:
                                try:
                                    self.target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                    self.target_socket.connect((self.target_host, self.target_port))
                                    logging.info("Reconnected to target.")
                                    reconnected = True
                                    # We don't reset inputs because target_socket is updated
                                except:
                                    time.sleep(backoff)
                                    backoff = min(backoff * 2, 30)
                            
                            if not reconnected:
                                self.running = False # Give up
                            break # Break the inner loop to refresh select() with new socket
                        
                        if not self.init_buffer_done:
                            self.init_buffer += data

                        # Broadcast to all clients
                        for client in self.clients[:]:
                            try:
                                client.sendall(data)
                            except:
                                if client in self.clients:
                                    self.clients.remove(client)
                                try:
                                    client.close()
                                except:
                                    pass
                    except Exception as e:
                        logging.error(f"Error reading from target: {e}")
                        # We should probably attempt reconnect here too, but for simplicity let's break
                        # and let the user restart if it's a hard fail. 
                        # Or better, treating it as a disconnect:
                        self.target_socket.close()
                        # Simple reconnect attempt (blocking) - ideally this would be async but 
                        # blocking here for a few seconds is better than crashing
                        try:
                            time.sleep(5)
                            self.target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            self.target_socket.connect((self.target_host, self.target_port))
                            logging.info("Reconnected to target after error.")
                        except:
                            logging.error("Failed to reconnect immediately.")

                else:
                    # Data from a client
                    try:
                        data = sock.recv(4096)
                        if not data:
                            if sock in self.clients:
                                self.clients.remove(sock)
                            sock.close()
                        else:
                            # Forward to target
                            try:
                                self.target_socket.sendall(data)
                            except Exception as e:
                                logging.error(f"Error sending to target: {e}. Attempting to reconnect...")
                                # Force a reconnection attempt
                                try:
                                    self.target_socket.close()
                                except:
                                    pass
                                
                                # Reconnect logic
                                reconnected = False
                                backoff = 1
                                while self.running and not reconnected:
                                    try:
                                        self.target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                        self.target_socket.connect((self.target_host, self.target_port))
                                        logging.info("Reconnected to target successfully.")
                                        # Resend the data that failed
                                        self.target_socket.sendall(data)
                                        reconnected = True
                                    except Exception as ex:
                                        logging.error(f"Reconnect failed: {ex}. Retrying in {backoff}s...")
                                        time.sleep(backoff)
                                        backoff = min(backoff * 2, 10)
                                
                                if not reconnected:
                                    self.running = False
                    except:
                        if sock in self.clients:
                            self.clients.remove(sock)
                        try:
                            sock.close()
                        except:
                            pass

        # Cleanup
        if self.server_socket: 
            try: self.server_socket.close()
            except: pass
        if self.target_socket: 
            try: self.target_socket.close()
            except: pass
        for c in self.clients: 
            try: c.close()
            except: pass
