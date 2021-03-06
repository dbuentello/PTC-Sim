""" PTC-Sim's messaging library for sending and receiving Edge Message 
    Protocol(EMP) messages over TCP/IP. 

    Message Specification:
        EMP V4 (specified in msg_spec/S-9354.pdf) fixed-format messages 
        with variable-length header sections.
        See README.md for more info.

    Author: Dustin Fast, 2018
"""

import Queue
import socket
import datetime
from time import sleep
from binascii import crc32
from threading import Thread
from struct import pack, unpack
from multiprocessing import Process
from ConfigParser import RawConfigParser

from lib_app import broker_log
from lib_app import REFRESH_TIME

# Import conf data
config = RawConfigParser()
config.read('config.dat')
BROKER = config.get('messaging', 'broker')
BOS_EMP = config.get('messaging', 'bos_emp_addr')
SEND_PORT = int(config.get('messaging', 'send_port'))
FETCH_PORT = int(config.get('messaging', 'fetch_port'))
MAX_MSG_SIZE = int(config.get('messaging', 'max_msg_size'))
NET_TIMEOUT = float(config.get('messaging', 'network_timeout'))
MSG_INTERVAL = float(config.get('messaging', 'msg_interval'))
LOCO_EMP_PREFIX = config.get('messaging', 'loco_emp_prefix')

# Set default timeout for all sockets, including importers of this library
socket.setdefaulttimeout(NET_TIMEOUT)


class Message(object):
    """ A representation of a message, including it's raw EMP form. Contains
        static functions for converting between tuple and raw EMP form.
    """

    def __init__(self, msg_content):
        """ Constructs a message object from the given content - either a
            well-formed EMP msg string, or a tuple of the form:
                (Message Type - ex: 6000,
                 Sender address - ex: 'arr.b:locop',
                 Destination address - ex: 'arr.l.arr.IDNM',
                 Payload - ex: { key: value, ... }
                )
                Note: All other EMP fields are static in this implementation.
        """
        if type(msg_content) == str:
            self.raw_msg = msg_content
            msg_content = self._to_tuple(msg_content)
        else:
            if type(msg_content) != tuple or len(msg_content) != 4:
                raise Exception('Msg content is an unexpected type or length.')
            self.raw_msg = self._to_raw(msg_content)

        self.msg_type = msg_content[0]
        self.sender_addr = msg_content[1]
        self.dest_addr = msg_content[2]
        self.payload = msg_content[3]

    @staticmethod
    def _to_raw(msg_tuple):
        """ Given a msg in tuple form, returns a well-formed EMP msg string.
        """
        msg_type = msg_tuple[0]
        sender_addr = msg_tuple[1]
        dest_addr = msg_tuple[2]
        payload = msg_tuple[3]
        payload_str = str(payload)

        # Calculate body size (i.e. payload length + room for the 32 bit CRC)
        body_size = 4 + len(payload_str)

        # Calculate size of variable portion of the "Variable Header",
        # i.e. len(source and destination strings) + null terminators.
        var_headsize = len(sender_addr) + len(dest_addr) + 2

        # Build the raw msg msg using struct.pack, noting that
        #   B = unsigned char, 8 bits
        #   H = unsigned short, 16 bits
        #   I = unsigned int, 32 bits
        #   i = signed int, 32 bits
        try:
            # Pack EMP "Common Header"
            raw_msg = pack(">B", 4)  # 8 bit EMP header version
            raw_msg += pack(">H", msg_type)  # 16 bit message type/ID
            raw_msg += pack(">B", 1)  # 8 bit message version
            raw_msg += pack(">B", 0)  # 8 bit flag, all zeroes here.
            raw_msg += pack(">I", body_size)[1:]  # 24 bit msg body size

            # Pack EMP "Variable Header"
            # 8 bit variable header size
            raw_msg += pack(">B", var_headsize)
            raw_msg += pack(">H", 120)  # 16 bit network TTL (seconds)
            raw_msg += pack(">H", 0)  # 16 bit QoS, 0 = no preference
            raw_msg += sender_addr  # 64 byte (max) msg source addr string
            raw_msg += '\x00'  # null terminate msg source address
            raw_msg += dest_addr  # 64 byte (max) msg dest addr string
            raw_msg += '\x00'  # null terminate destination address

            # Pack msg body
            raw_msg += payload_str  # Variable size
            raw_msg += pack(">i", crc32(raw_msg))  # 32 bit CRC
        except:
            raise Exception("Msg format is invalid")

        return raw_msg

    @staticmethod
    def _to_tuple(raw_msg):
        """ Returns a tuple representation of the msg contained in raw_msg.
        """
        # Validate raw_msg
        if not raw_msg or len(raw_msg) < 20:  # 20 byte min msg size
            raise Exception("Invalid message format")

        # Ensure good CRC
        msg_crc = unpack(">i", raw_msg[-4::])[0]  # last 4 bytes
        raw_crc = crc32(raw_msg[:-4])

        if msg_crc != raw_crc:
            raise Exception("CRC Mismatch - message may be corrupt.")

        # Unpack msg fields, noting that unpack returns results as a tuple
        msg_type = unpack('>H', raw_msg[1:3])[0]  # bytes 1-2
        vhead_size = unpack('>B', raw_msg[8:9])[0]  # byte 8

        # Extract sender, destination, and playload based on var header size
        vhead_end = 13 + vhead_size
        vhead = raw_msg[13:vhead_end]
        vhead = vhead.split('\x00')  # split on terminators for easy extract
        sender_addr = vhead[0]
        dest_addr = vhead[1]
        payload = raw_msg[vhead_end:len(raw_msg) - 4]  # -4 moves before CRC

        # Turn the payload into a python dictionary
        try:
            payload = eval(payload)
        except:
            raise Exception('Msg payload not of form { key: value, ... }')

        return (msg_type, sender_addr, dest_addr, payload)


class Connection(object):
    """ An abstraction of a communication interface. Ex: A 220 MHz radio
        connection. Contains a messaging client and a thread that disconnects
        on timeout.
        Note: This class is nominal and for sim purposes only at this point -
        no actual TCP/IP or EMP addressing here.
    """
    def __init__(self, ID, timeout=0):
        """ self.ID             : (str) The interfaces unique ID/address
            self.last_activity  : (datetime) Time of last activity
            self.client         : (Client) The interfaces messaging client
            self.Receiver       : (Receiver) Incoming TCP/IP connection watcher
            self.conn_to   : (TrackDevice) Active connection partner

            self._timeout_seconds: (int) Seconds of inactivity before timeout
            self._timeout_watcher: A thread. Updates self.active on timeout
        """
        # Properties
        self.ID = ID
        self.last_activity = None
        # TODO: self.transport_class. i.e. radio, etc.

        # Interface
        self.conn_to = None
        self.client = Client()
        # TODO: self.receiver = Receiver()

        # Timeout
        self._timeout = timeout
        self.timeout_watcher = Thread(target=self._timeoutwatcher)
        # self.timeout_watcher.start()

    def __str__(self):
        """ Returns a string representation of the base station """
        ret_str = 'Connection' + self.ID + ': '
        ret_str += {True: 'Active', False: 'Inactive'}.get(self.active)

        return ret_str

    def send(self, message):
        """ Sends the given message over the connection's interface. Also
            updates keep alive.
        """
        self.client.send_msg(message)
        self.keep_alive()

    def fetch(self, queue_name):
        """ Fetches the next message from the given queue at the broker and
            returns it. Also updates keep alive.
        """
        self.client.fetch_next_msg(queue_name)
        self.keep_alive()

    def keep_alive(self):
        """ Update the last activity time to prevent timeout.
        """
        self.active = True
        self.last_activity = datetime.datetime.now()

    def connect(self, obj):
        """ Establishes the connection (nominally, at this point), to the
            given TrackDevice.
        """
        self.conn_to = obj
        self.keep_alive()

    def connected(self):
        """ Returns True if connection is connected, else returns False
        """
        if self.conn_to:
            return True
        return False

    def disconnect(self):
        """ "Terminates" the nominal connection
        """
        self.conn_to = None

    def _timeoutwatcher(self):
        """ Resets the connections 'active' flag if timeout elapses
            Intended to run as a thread.
        """
        while True:
            if not self.last_activity:
                self.disconnect()
            elif self._timeout != 0:
                delta = datetime.timedelta(seconds=self._timeout)
                if delta < datetime.datetime.now() - self.last_activity:
                    self.disconnect()
            sleep(REFRESH_TIME)


class Client(object):
    """ Exposes send_msg() and fetch_msg() interfaces to broker clients.
    """

    def __init__(self,
                 broker=BROKER,
                 broker_send_port=SEND_PORT,
                 broker_fetch_port=FETCH_PORT):
        """
        """
        self.broker = broker
        self.send_port = broker_send_port
        self.fetch_port = broker_fetch_port

    def send_msg(self, message):
        """ Sends the given message (of type Message) over TCP/IP to the 
            broker. The msg will wait at the broker in the queue specified by
            the message to be fetched by other broker clients.
            Returns True if msg sent succesfully, else raises an issue-
            specific exception.
        """
        try:
            # Init socket and connect to broker
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self.broker, self.send_port))

            # Send message and wait for a response
            sock.send(message.raw_msg.encode('hex'))
            response = sock.recv(MAX_MSG_SIZE).decode()
            sock.close()
        except:
            raise Exception('Send Error: Could not connect to broker.')

        if response == 'OK':
            return True
        elif response == 'FAIL':
            raise Exception('Send Error: Broker responded with FAIL.')
        else:
            err_str = 'Send Error: Unhandled response received from broker.'
            raise Exception(err_str)

    def fetch_next_msg(self, queue_name):
        """ Fetches the next msg from queue_name from the broker and returns it,
            Raises Queue.Empty if specified queue is empty.
        """
        try:
            # Establish socket connection
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self.broker, self.fetch_port))

            # Send queue name and wait for response
            sock.send(queue_name.encode())
            resp = sock.recv(MAX_MSG_SIZE)
        except:
            raise Exception('Fetch Error: Could not connect to broker.')

        msg = None
        if resp == 'EMPTY':
            raise Queue.Empty  # No msg available to fetch
        else:
            msg = Message(resp.decode('hex'))  # Response is the msg

        sock.close()

        return msg


def get_6000_msg(loco):
        """ Returns a well-formed 6000 (loco status) msg for the given loco.
        """
        con_str = str({k: v.conn_to.ID for (k, v)
                       in loco.conns.iteritems()
                       if v.connected() is True})
                       
        status = {'loco': loco.ID,
                  'speed': loco.speed,
                  'heading': loco.heading,
                  'direction': loco.direction,
                  'milepost': loco.coords.marker,
                  'lat': loco.coords.lat,
                  'long': loco.coords.long,
                  'bpp': loco.bpp,
                  'conns': con_str}

        msg_type = 6000
        msg_source = loco.emp_addr
        msg_dest = BOS_EMP
        payload = status

        status_msg = Message((msg_type,
                              msg_source,
                              msg_dest,
                              payload))

        return status_msg


class Receiver(Thread):
    """ Watches for incoming EMP messages over TCP/IP on the interface and port 
        specified and adds them to the given list of queues (a list)
    """
    def __init__(self, outgoing_queues):
        Thread.__init__(self)
        self.outgoing_queues = outgoing_queues

    def run(self):
        # Init TCP/IP listener
        sock = socket.socket()
        sock.bind((BROKER, SEND_PORT))
        sock.listen(1)

        while True:
            # Block until timeout or a send request is received
            try:
                conn, client = sock.accept()
            except:
                continue

            # Receive the msg from sender, responding with either OK or FAIL
            log_str = 'Incoming msg from ' + str(client[0]) + ' gave: '
            try:
                raw_msg = conn.recv(MAX_MSG_SIZE).decode()
                msg = Message(raw_msg.decode('hex'))
                conn.send('OK'.encode())
                conn.close()
            except Exception as e:
                log_str += 'Msg recv failed due to ' + str(e)
                broker_log.error(log_str)

                try:
                    conn.send('FAIL'.encode())
                except:
                    pass

                conn.close()
                continue

            # Add msg to outgoing queue dict, keyed by dest_addr
            if not self.outgoing_queues.get(msg.dest_addr):
                self.outgoing_queues[msg.dest_addr] = Queue.Queue()
            self.outgoing_queues[msg.dest_addr].put(msg)
            log_str = 'Msg served: ' + msg.sender_addr + ' '
            log_str += 'to ' + msg.dest_addr
            broker_log.info(log_str)

        # Do cleanup
        sock.close()


class MsgServer(Thread):
    """ Watches for incoming TCP/IP msg requests (ex, A loco or the BOS
        checking its msg queue) and serves them from the given list of queues 
        by address.
        After a msg is served it's removed from the queue.
    """
    def __init__(self, outgoing_queues):
        Thread.__init__(self)
        self.outgoing_queues = outgoing_queues

    def run(self):
        # Init listener
        sock = socket.socket()
        sock.bind((BROKER, FETCH_PORT))
        sock.listen(1)

        while True:
            # Block until timeout or a send request is received
            try:
                conn, client = sock.accept()
            except:
                continue

            # Process the request
            log_str = 'Fetch request from ' + str(client[0]) + ' '
            try:
                queue_name = conn.recv(MAX_MSG_SIZE).decode()
                log_str += 'for ' + queue_name + ' gave: '

                msg = None
                try:
                    msg = self.outgoing_queues[queue_name].get(timeout=.5)
                except:
                    log_str += 'Queue empty.'
                    conn.send('EMPTY'.encode())

                if msg:
                    conn.send(msg.raw_msg.encode('hex'))  # Send msg
                    log_str += 'Msg served.'

                broker_log.info(log_str)
                conn.close()
            except:
                continue

        # Do cleanup
        sock.close()


class MsgBroker(Process):
    """ PTC-Sim's Edge Message Protocol (EMP) Message Broker.
    Msgs are received by the broker via TCP/IP and enqued for receipt.
    Recipients request msgs from broker via TCP/IP by address (i.e queue name).
    After a fetch, the msg is removed from the queue.
    """
    def __init__(self): 
        Process.__init__(self)
        self.outgoing_queues = {}  # Outbound msg queues: { ADDRESS: Queue }

    def run(self):        
        Receiver(self.outgoing_queues).start()
        MsgServer(self.outgoing_queues).start()
        broker_log.info('BOS Started.')

        # Stay alive (there is likely a better way to do this)
        while True:
            sleep(10)


# debug:
# if __name__ == '__main__':
#     # Start the track simulation in terminal mode
#     sim = MsgBroker()
#     sim.start()
