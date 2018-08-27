#!/usr/bin/env python
""" loco.py - Simulates a locomotive traveling on a railroad track and
    sending/receiving status/command messages. See README.md for more info.

    Author: Dustin Fast, 2018
"""

from time import sleep
from threading import Thread

from lib_app import Prompt, track_log
from lib_track import Track

from lib_app import APP_NAME, REFRESH_TIME

class TrackSim(object):
    """ The Track Simulator. Consists of three seperate threads:
        Msg Receiver  - Watchers for incoming messages over TCP/IP.
        Fetch Watcher - Watches for incoming fetch requests over TCP/IP and
                        serves msgs as appropriate.
    """
    def __init__(self):
        self.running = False  # Thread kill flag
        self.tracksim_thread = Thread(target=self._tracksim)

    def start(self, terminal=False):
        """ Start the message broker threads
        """
        if not self.running:
            track_log.info('Track Sim Starting...')
            self.running = True
            self.tracksim_thread.start()

            # If we're not running from the terminal, chill while threads run.
            if not terminal:
                self.tracksim_thread.join()

    def stop(self):
        """ Stops the msg broker. I.e., the msg receiver and fetch watcher 
            threads. 
        """
        if self.running:
            # Signal stop to thread and join
            self.running = False
            self.tracksim_thread.join(timeout=REFRESH_TIME)

            # Redefine thread, to allow starting after stopping
            self.tracksim_thread = Thread(target=self._tracksim)

            track_log.info('Track Sim stopped.')

    def _tracksim(self):
        """ The Track simulator - Simulates locomotives
            traveling on a track. # TODO: Implement bases, switches, etc.
        """
        # Instantiate the Track - It contains all devices and locos.
        ptctrack = Track()

        # Start each track componenet-device's simulation thread
        # These devices exists "on" the track and simulate their own 
        # operation.
        for l in ptctrack.locos.values():
            l.sim.start()
        
        while self.running:
            for l in ptctrack.locos.values():
                status_str = 'Loco ' + l.ID + ': '
                status_str += str(l.speed) + ' @ ' + str(l.coords.marker)
                status_str += ' (' + str(l.coords.long) + ',' + str(l.coords.lat) + ')'
                status_str += '. Bases in range: '
                status_str += ', '.join([b.ID for b in l.bases_inrange])
                status_str += ' Conns: '
                status_str += ', '.join([c.conn_to.ID for c in l.conns.values() if c.conn_to])
                track_log.info(status_str)

            sleep(REFRESH_TIME)

        # Stop each device's sim thread.
        print('Stopping sims... Please wait.')
        for l in ptctrack.locos.values():
            l.sim.stop()


if __name__ == '__main__':
    # Start the track simulation in terminal mode
    print("-- " + APP_NAME + ": Track Simulator - Type 'exit' to quit --\n")
    sim = TrackSim()
    sim.start(terminal=True)    
    Prompt(sim).get_repl().start()  # Blocks until 'exit' cmd received.
