#!/usr/bin/env python
""" PTC-Sim's Back Office Server (BOS). 
    Publishes the PTC-Sim web interface via Flask and watches the message
    broker (via TCP/IP) for device and locomotive status msgs. The web display 
    is then updated to reflect these statuses, including Google Earth location
    mapping. 
    The BOS may also also send outgoing computer-aided-dispatch (CAD) msgs to
    each device.

    Author: Dustin Fast, 2018
"""

from time import sleep
from threading import Thread
    
from lib_app import bos_log, dep_install
from lib_messaging import Client, queue
from lib_track import Track, Loco, Location
from lib_web import get_locos_table, get_status_map, get_tracklines, get_loco_connlines

from lib_app import APP_NAME, REFRESH_TIME
from lib_messaging import BROKER, SEND_PORT, FETCH_PORT, BOS_EMP

# Attempt to import 3rd party modules and prompt for install on fail
try:
    from flask import Flask, render_template, jsonify, request
except:
    dep_install('Flask')
try:
    from flask_googlemaps import GoogleMaps
except:
    dep_install('flask_googlemaps')

# Init Flask and Google Maps Flask module
bos_web = Flask(__name__)
GoogleMaps(bos_web, key="AIzaSyAcls51x9-GhMmjEa8pxT01Q6crxpIYFP0")

# Global web state vars
g_locos_table = 'Error populating table.'
g_status_maps = {}  # { None: all_locos_statusmap, loco.name: loco_statusmap }
g_conn_lines = {}  # { TrackDevice.name: loco_statusmap }


##############################
# Flask Web Request Handlers #
##############################

@bos_web.route('/' + APP_NAME)
def home():
    """ Serves home.html
    """
    return render_template('home.html', status_map=g_status_maps[None])

@bos_web.route('/_home_get_async_content', methods=['POST'])
def _home_get_async_content():
    """ Serves the asynchronous content, such as the locos table and status map.
    """
    try:
        loco_name = request.json['loco_name']

        if loco_name:
            return jsonify(status_map=g_status_maps[loco_name].as_json(),
                           locos_table=g_locos_table,
                           loco_connlines=g_conn_lines.get(loco_name))
        else:
            return jsonify(status_map=g_status_maps[None].as_json(),
                           locos_table=g_locos_table)
    except Exception as e:
        bos_log.error(e)
        return 'error' 


#############
# BOS Class #
#############

class BOS(object):
    """ The Back Office Server. Consists of a messaging client and status
        watcher thread that fetches messages from the broker over TCP/IP, in
        addition to the web interface.
    """
    def __init__(self):
        self.track = Track()

        # Messaging client
        self.msg_client = Client(BROKER, SEND_PORT, FETCH_PORT)

        # Threading
        self.running = False  # Thread kill flag
        self.msg_watcher_thread = Thread(target=self._statuswatcher)
        self.webupdate_thread = Thread(target=self._webupdater)

    def start(self, debug=False):
        """ Start the BOS. I.e., the status watcher thread and web interface.
        """
        bos_log.info('BOS Starting.')
        
        self.running = True
        self.msg_watcher_thread.start()
        self.webupdate_thread.start()
        sleep(.5)  # Ample time to ensure all threads did one iteration

        bos_web.run(debug=True, use_reloader=False)  # Blocks until CTRL+C

        # Do shutdown
        print('\nBOS Stopping... Please wait.')
        self.running = False
        self.msg_watcher_thread.join(timeout=REFRESH_TIME)
        self.webupdate_thread.join(timeout=REFRESH_TIME)
        bos_log.info('BOS stopped.')

    def _statuswatcher(self):
        """ The status message watcher thread - watches the broker for msgs
            addressed to it and processes them.
        """
        while self.running:
            # Fetch the next available msg, if any
            msg = None
            try:
                msg = self.msg_client.fetch_next_msg(BOS_EMP)
            except queue.Empty:
                bos_log.info('Msg queue empty.')
            except Exception:
                bos_log.warn('Could not connect to broker.')

            # Process loco status msg. Msg should be of form given in 
            # docs/app_messaging_spec.md, Msg ID 6000.
            if msg and msg.msg_type == 6000:
                try:
                    locoID = msg.payload['loco']
                    location = Location(msg.payload['milepost'],
                                        msg.payload['lat'],
                                        msg.payload['long'])
                    active_conns = eval(msg.payload['conns'])  # evals to dict

                    # Eiter reference or instantiate loco with the given ID
                    loco = self.track.locos.get(locoID)
                    if not loco:
                        loco = Loco(locoID, self.track)

                    # Update the BOS's loco object with status msg params
                    loco.update(msg.payload['speed'],
                                msg.payload['heading'],
                                msg.payload['direction'],
                                location,
                                msg.payload['bpp'],
                                active_conns)

                    # Update the last seen time for this loco
                    self.track.set_lastseen(loco)
                    
                    bos_log.info('Processed status msg for ' + loco.name)
                except KeyError:
                    bos_log.error('Malformed status msg: ' + str(msg.payload))
            elif msg:
                bos_log.error('Fetched unhandled msg type: ' + str(msg.msg_type))
                
            sleep(REFRESH_TIME)
        
    def _webupdater(self):
        """ The web updater thread. Parses the BOS's local track object's
            devices and updates the web output (HTML table, Google Earth/KMLs, 
            etc.) accordingly.
        """
        global g_locos_table, g_status_maps, g_conn_lines

        while self.running:
            # Update g_locos_table and main panel map
            g_locos_table = get_locos_table(self.track)

            # Get updated map lines
            tracklines = get_tracklines(self.track)
            g_conn_lines = get_loco_connlines(self.track)
            maps = {}  # Temporary container, so we never serve incomplete map

            for loco in self.track.locos.values():
                maps[loco.name] = get_status_map(self.track, tracklines, loco)
            maps[None] = get_status_map(self.track, tracklines)  

            g_status_maps = maps

            sleep(REFRESH_TIME)


if __name__ == '__main__':
    # Start the Back Office Server
    print('-- ' + APP_NAME + ': Back Office Server - CTRL + C quits --\n')
    sleep(.2)  # Ensure welcome statment outputs before flask output
    bos = BOS().start(debug=True)
