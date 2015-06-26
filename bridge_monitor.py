#!/usr/bin/env python
# bridge_monotor.py
# Copyright (C) ContinuumBridge Limited, 2015 - All Rights Reserved
# Unauthorized copying of this file, via any medium is strictly prohibited
# Proprietary and confidential
# Written by Peter Claydon
#
"""
Just stick actions from incoming requests into threads.
"""

import json
import requests
import time
import sys
import os.path
import signal
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.MIMEImage import MIMEImage
import logging
import logging.handlers
import twilio
import twilio.rest
from autobahn.twisted.websocket import WebSocketClientFactory, WebSocketClientProtocol, connectWS
from twisted.internet import threads
from twisted.internet import reactor, defer
from twisted.internet import task
from twisted.internet.protocol import ReconnectingClientFactory

config                = {}
bridges               = []
HOME                  = os.path.expanduser("~")
CONFIG_FILE           = HOME + "/bridge_monitor/bridge_monitor.config"
CB_LOGFILE            = HOME + "/bridge_monitor/monitor.log"
CB_ADDRESS            = "portal.continuumbridge.com"
CB_LOGGING_LEVEL      = "DEBUG"
CONFIG_READ_INTERVAL  = 10.0
WATCHDOG_TIME         = 60 * 61  # If not heard about a bridge for this time, consider it disconnected
MONITOR_INTERVAL      = 30       # How often to run watchdog code
 
logger = logging.getLogger('Logger')
logger.setLevel(CB_LOGGING_LEVEL)
handler = logging.handlers.RotatingFileHandler(CB_LOGFILE, maxBytes=10000000, backupCount=5)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

def nicetime(timeStamp):
    localtime = time.localtime(timeStamp)
    milliseconds = '%03d' % int((timeStamp - int(timeStamp)) * 1000)
    now = time.strftime('%H:%M:%S, %d-%m-%Y', localtime)
    return now

def sendMail(to, subject, body):
    try:
        user = config["mail"]["user"]
        password = config["mail"]["password"]
        # Create message container - the correct MIME type is multipart/alternative.
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = config["mail"]["from"]
        recipients = to.split(',')
        [p.strip(' ') for p in recipients]
        if len(recipients) == 1:
            msg['To'] = to
        else:
            msg['To'] = ", ".join(recipients)
        # Create the body of the message (a plain-text and an HTML version).
        text = body + " \n"
        htmlText = text
        # Record the MIME types of both parts - text/plain and text/html.
        part1 = MIMEText(text, 'plain')
        part2 = MIMEText(htmlText, 'html')
        msg.attach(part1)
        msg.attach(part2)
        mail = smtplib.SMTP('smtp.gmail.com', 587)
        mail.ehlo()
        mail.starttls()
        mail.login(user, password)
        mail.sendmail(user, recipients, msg.as_string())
        logger.debug("Sent mail")
        mail.quit()
    except Exception as ex:
        logger.warning("sendMail problem. To: %s, type %s, exception: %s", to, type(ex), str(ex.args))
       
def postData(dat, bid):
    try:
        url = ""
        for b in config["bridges"]:
            if b["bid"] == bid:
                if "database" in b:
                    url = config["dburl"] + "db/" + b["database"] + "/series?u=root&p=" + config["dbrootp"]
                else:
                    url = config["dburl"] + "db/Bridges/series?u=root&p=27ff25609da60f2d"
                break
        headers = {'Content-Type': 'application/json'}
        status = 0
        logger.debug("url: %s", url)
        r = requests.post(url, data=json.dumps(dat), headers=headers)
        status = r.status_code
        if status !=200:
            logger.warning("POSTing failed, status: %s", status)
    except Exception as ex:
        logger.warning("postData problem, type %s, exception: %s", to, type(ex), str(ex.args))

def sendSMS(messageBody, to):
    numbers = to.split(",")
    for n in numbers:
       try:
           client = twilio.rest.TwilioRestClient(config["twilio_account_sid"], config["twilio_auth_token"])
           message = client.messages.create(
               body = messageBody,
               to = n,
               from_ = config["twilio_phone_number"]
           )
           sid = message.sid
           logger.debug("Sent sms: %s", str(n))
       except Exception as ex:
           logger.warning("sendSMS, unable to send message %s to: %s, type %s, exception: %s", messageBody, str(to), type(ex), str(ex.args))

def readConfig(forceRead=False):
    try:
        if time.time() - os.path.getmtime(CONFIG_FILE) < 600 or forceRead:
            global config
            with open(CONFIG_FILE, 'r') as f:
                newConfig = json.load(f)
                config.update(newConfig)
            for c in config:
                if c.lower in ("true", "t", "1"):
                    config[c] = True
                elif c.lower in ("false", "f", "0"):
                    config[c] = False
            #logger.info("Read new config: " + json.dumps(config, indent=4))
    except Exception as ex:
        logger.warning("Problem reading bridge_monitor.config, type: %s, exception: %s", str(type(ex)), str(ex.args))

def readConfigLoop():
    #logger.debug("readConfigLoop")
    readConfig(True)
    configLoop = reactor.callLater(CONFIG_READ_INTERVAL, readConfigLoop)

def authorise():
    try:
        auth_url = "http://" + CB_ADDRESS + "/api/client/v1/client_auth/login/"
        auth_data = '{"key": "' + config["cid_key"] + '"}'
        auth_headers = {'content-type': 'application/json'}
        response = requests.post(auth_url, data=auth_data, headers=auth_headers)
        cbid = json.loads(response.text)['cbid']
        sessionID = response.cookies['sessionid']
        ws_url = "ws://" + CB_ADDRESS + ":7522/"
        return cbid, sessionID, ws_url
    except Exception as ex:
        logger.warning("Unable to authorise with server, type: %s, exception: %s", str(type(ex)), str(ex.args))
    
def readConfig(forceRead=False):
    try:
        if time.time() - os.path.getmtime(CONFIG_FILE) < 600 or forceRead:
            global config
            with open(CONFIG_FILE, 'r') as f:
                newConfig = json.load(f)
                config.update(newConfig)
            for c in config:
                if c.lower in ("true", "t", "1"):
                    config[c] = True
                elif c.lower in ("false", "f", "0"):
                    config[c] = False
            #logger.info("Read new config: " + json.dumps(config, indent=4))
    except Exception as ex:
        logger.warning("Problem reading monitor.config, type: %s, exception: %s", str(type(ex)), str(ex.args))

class ClientWSFactory(ReconnectingClientFactory, WebSocketClientFactory):
    maxDelay = 60
    maxRetries = 200
    def startedConnecting(self, connector):
        logger.debug('Started to connect.')
        ReconnectingClientFactory.resetDelay

    def clientConnectionLost(self, connector, reason):
        logger.debug('Lost connection. Reason: %s', reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        logger.debug('Lost reason. Reason: %s', reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)

class ClientWSProtocol(WebSocketClientProtocol):
    def __init__(self):
        logger.debug("Connection __init__")
        signal.signal(signal.SIGINT, self.signalHandler)  # For catching SIGINT
        signal.signal(signal.SIGTERM, self.signalHandler)  # For catching SIGTERM
        self.stopping = False
        l = task.LoopingCall(self.monitor)
        l.start(MONITOR_INTERVAL)

    def signalHandler(self, signal, frame):
        logger.debug("signalHandler received signal")
        self.stopping = True
        reactor.stop()

    def sendAck(self, ack):
        self.sendMessage(json.dumps(ack))

    def onConnect(self, response):
        logger.debug("Server connected: %s", str(response.peer))

    def onOpen(self):
        logger.debug("WebSocket connection open.")

    def onClose(self, wasClean, code, reason):
        logger.debug("onClose, reason:: %s", reason)

    def onMessage(self, message, isBinary):
        #logger.debug("onMessage")
        try:
            msg = json.loads(message)
            #logger.info("Message received: %s", json.dumps(msg, indent=4))
        except Exception as ex:
            logger.warning("onmessage. Unable to load json, type: %s, exception: %s", str(type(ex)), str(ex.args))
            return
        if not "body" in msg:
            logger.warning("monitor. onmessage. message without body")
            return
        if msg["body"] == "connected":
            logger.info("Connected to ContinuumBridge")
            return
        if not "source" in msg:
            logger.warning("monitor. onmessage. message without source")
            return
        else:
            # Not an efficient way of doing it, but I'm in a hurry
            found = False
            for b in bridges:
                if msg["source"] == b["name"]:
                    b["active"] = True,
                    b["time"] = time.time()
                    if "version" in msg["body"]:
                        b["version"] = msg["body"]["version"]
                    else:
                        b["version"] = "not reported"
                    if "up_since" in msg["body"]:
                        b["up_since"] = msg["body"]["up_since"]
                    else:
                        b["up_since"] =-1 
                    found = True
                    logger.info("Message from bridge: %s. Version: %s. Up since: %s", msg["source"].split('/')[0], \
                        b["version"], "not reported" if b["up_since"] == -1 else nicetime(b["up_since"])) 
                    break
            if not found:
                bridges.append({
                    "name": msg["source"], 
                    "active": True,
                    "time": time.time(),
                    "version": msg["body"]["version"] if "version" in msg["body"] else "not reported",
                    "up_since": msg["body"]["up_since"] if "up_since" in msg["body"] else -1 
                })
                logger.info("New bridge: %s. Version: %s. Up since: %s", msg["source"].split('/')[0], bridges[-1]["version"], \
                    "not reported" if bridges[-1]["up_since"] == -1 else nicetime(bridges[-1]["up_since"]))
            ack = {
                    "source": config["cid"],
                    "destination": msg["source"],
                    "body": {"command": "none"}
                  }
            #logger.debug("onMessage ack: %s", str(json.dumps(ack, indent=4)))
            logger.info("Sent ack to %s", msg["source"].split('/')[0])
            reactor.callInThread(self.sendAck, ack)

    def monitor(self):
        if bridges:
            now = time.time()
            for b in bridges:
                if now - b["time"] > WATCHDOG_TIME:
                    if b["active"]:
                        b["active"] = False
                        name =  b["name"].split('/')[0]
                        alert = "Not heard from bridge " + name + " since " + nicetime(b["time"])
                        subject = "Alert for Bridge " + name
                        reactor.callInThread(sendMail, config["email"], subject, alert)

if __name__ == '__main__':
    readConfig(True)
    cbid, sessionID, ws_url = authorise()
    headers = {'sessionID': sessionID}
    factory = ClientWSFactory(ws_url, headers=headers, debug=False)
    factory.protocol = ClientWSProtocol
    connectWS(factory)
    readConfigLoop()
    reactor.run()
