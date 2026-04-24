import asyncio
import re
from enum import Enum
import telnetlib3
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
import uvicorn
import logging

# ---------------- CONFIG ----------------
TUNNEL_PORT = 3300
CONNECT_TIMEOUT = 5
CMD_TIMEOUT = 10
NUMBER_OF_MEASUREMENTS = 2   #number of current measurements to find the average

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.DEBUG)  # Set the global logging level

# ------------------ INIT -----------------

app = FastAPI()
running = False
stopping = False


# ---------------- TELNET CLIENT ----------------
class TelnetClient:
    def __init__(self):
        self.reader = None
        self.writer = None

    #start a telnet connection
    async def connect(self,ip,port):
        if self.reader:
            return

        try:
            self.reader, self.writer = await asyncio.wait_for(telnetlib3.open_connection(ip,port), timeout=CONNECT_TIMEOUT)
            logging.debug("Telnet connected.")
        except asyncio.TimeoutError:
            logging.error("Couldn't establish Telnet connection.")
            raise

    #end a telnet connection
    async def disconnect(self):
        self.writer.close()
        await self.writer.wait_closed()
        self.reader = None
        self.writer = None
        logging.debug("Telnet disconnected.")

    #send a command to telnet and wait for a response (or timeout)
    async def send_and_wait(self, cmd, linesExpected, timeout=CMD_TIMEOUT):
        #send the command
        self.writer.write(cmd + "\r\n")

        lines = []

        #waiting for the response
        while True:

            #try to read a line or timeout
            try:
                line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
                if not line:  #connection closed
                    break
                if line=='\n':   #sometimes the \n appears in a separate line by itself - append it to the previous line
                    if len(lines)>0:
                        logging.info("A \\n was transmitted separately from \\r.")
                        lines[-1]+='\n'
                else:
                    lines.append(line)
            except asyncio.TimeoutError:
                break

            #break, if the expected number of lines were sent
            if len(lines)==linesExpected:
                break

        return lines


# ---------------- CONTROLLER (STATE MACHINE) ----------------
class Controller:
    def __init__(self, telnet):
        self.telnet = telnet

    async def switchFeb(self, feb):
        try:
            self.telnet.writer.write(f"lp {feb}\r\n")  #set FEB
            responses = await self.telnet.send_and_wait(f"lp",4,2)  #use the response to see, if the correct FEB was set and/or the FEB inactive
            response = "".join(responses)                           #the structure of the response is not predictable: it is not known which line has the list of ports
            activeFebs = re.findall(r"\((\d+)\)", response)  #find all numbers in parentheses. there should only be one: the active FEB
            logging.debug(f"Response after switching the FEBs: {response}")
            if len(activeFebs)!=1:
                return False
            if int(activeFebs[0])!=feb:
                return False
            return True
        except Exception as a:
            return False

    async def readChannel(self, fpga, channel):  #channel is the channel within an fpga
        try:
            response = await self.telnet.send_and_wait(f"lc mux {fpga}",1)  #set mux
            expectedResponse = ["MuxFPGA0-3="+str(fpga)+"\r\n"]
            if response!=expectedResponse:
                logging.debug(f"Unexpected response after setting mux for FPGA {fpga}: {response}")
                return None
            if fpga==0:
                arg1=format(channel,"x")
                self.telnet.writer.write(f"lc wr 20 1{arg1}\r\n")
            else:
                cmbAtFpga=channel//4   #floor division
                channelAtCmb=channel%4
                arg1=format(fpga*4,"x")
                arg2=format(cmbAtFpga*4,"x")
                arg3=format(channelAtCmb,"x")
                self.telnet.writer.write(f"lc wr {arg1}20 1{arg2}\r\n")
                self.telnet.writer.write(f"lc wr 20 1{arg3}\r\n")
            self.telnet.writer.write(f"lc gain 8\r\n")
            response = await self.telnet.send_and_wait(f"lc A0 {NUMBER_OF_MEASUREMENTS}",NUMBER_OF_MEASUREMENTS+1)
            logging.debug(f"Response after current measurement for FPGA {fpga} / channel {channel}: {response}")
            if len(response)!=NUMBER_OF_MEASUREMENTS+1:
                logging.info(f"Unexpected number of current measurements for FPGA {fpga} / channel {channel}: {response}")
                return None
            arg1=format(fpga*4,"x")
            self.telnet.writer.write(f"lc wr {arg1}20 0\r\n") #disable mux

            averageLine = response[-1]
            if "avg" not in averageLine:
                logging.info(f"Average line not present in current measurement for FPGA {fpga} / channel {channel}")
                return None
            try:
                adc = float(averageLine.split(maxsplit=1)[0].replace("\x00",""))  #the string starts with '\x00'
            except (ValueError, IndexError):
                logging.info(f"Average current has invalid format for FPGA {fpga} / channel {channel}: {averageLine}")
                return None

            if adc > 4.096:
                adc = 8.192 - adc
            current = adc / 8 * 250;
            return current

        except Exception as a:
            logging.warning(f"Unknown error when measuring the current for FPGA {fpga} / channel {channel}")
            logging.warning(str(a))
            return None

    async def run(self, feb, ws: WebSocket):
        global running
        global stopping

        #switch the FEB
        if feb>=1 and feb<=24:
            response = await self.switchFeb(feb)
            if response is False:  #try one more time
                logging.info(f"Failed to switch to FEB {feb}. Waiting 5s before trying it again.")
                await asyncio.sleep(5)
                response = await self.switchFeb(feb)
            if response is False:  #still not switched
                logging.error(f"Failed to switch to FEB {feb} after 2nd attempt. Stopping.")
                await ws.send_json({
                    "type": "error",
                    "message": "inactive/unresponsive FEB",
                })
                return
        else:
            logging.error(f"Invalid FEB: {feb}. Stopping.")
            await ws.send_json({
                    "type": "error",
                    "message": "invalid FEB",
            })
            return

        await ws.send_json({
           "type": "status",
           "message": f"switched to FEB {feb} - taking data",
        })

        #loop through all channels
        for fpga in range(4):
            if stopping:  #stop button was pressed
                break
            for channel in range(16):
                if stopping:  #stop button was pressed
                    break
                result = await self.readChannel(fpga,channel)
                if result is None:    #try one more time
                    logging.info(f"Couldn't read FPGA {fpga} / channel {channel}. Waiting 5s before trying it again.")
                    await asyncio.sleep(5)
                    result = await self.readChannel(fpga,channel)

                if result is None:    #still not working
                    logging.error(f"Couldn't read FPGA {fpga} / channel {channel}. Moving to next channel.")
                    await ws.send_json({
                       "type": "result",
                       "channel": fpga*16+channel,
                       "value": "error"
                    })
                else:
                    logging.debug(f"Measured current of FPGA {fpga} / channel {channel}: {result} uA")
                    await ws.send_json({
                       "type": "result",
                       "channel": fpga*16+channel,
                       "value": result
                    })

        await self.telnet.disconnect()

        if not stopping:  #stop button was not pressed
           logging.debug("Finished taking data.")
           await ws.send_json({
              "type": "status",
              "message": "finished taking data",
           })
        else:
           logging.debug("Data taking was stopped by WebSocket client.")
           await ws.send_json({
              "type": "error",
              "message": "data taking was stopped",
           })

        stopping=False
        running=False


# ---------------- GLOBALS ----------------
#telnet
telnet = TelnetClient()
controller = Controller(telnet)

#start uvicorn server
if __name__ == "__main__":
    uvicorn.run("currentMeasurement:app", host="0.0.0.0", port=3300, reload=True)


# ----------------- WEBSITE -----------------
@app.get("/")
def index():
    logging.debug("localhost:3300 got a visitor. Opening the website.")
    return FileResponse("currentMeasurement.html")

# ---------------- WEBSOCKET ----------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global running
    global stopping

    await ws.accept()

    while True:
        msg = await ws.receive_json()

        if msg["type"] == "run":
            logging.debug("A run message was received.")
            if running:   #don't run it multiple times
                logging.warning("A current measurement is on going. Can't start a second one.")
                continue
            running=True
            ip = msg["ip"]
            port = msg["port"]
            feb = int(msg["feb"])
            try:
               await telnet.connect(ip,port)
            except asyncio.TimeoutError:
               await ws.send_json({"type": "error", "message": "Telnet connection timeout"})
               logging.error("Trying to establish a Telnet connection timed out.")
               running=False
               continue
            asyncio.create_task(controller.run(feb, ws))

        if msg["type"] == "stop":
            logging.debug("A stop message was received.")
            if not running:   #nothing to do, if it's not running
               logging.warning("No current measurement is on going. There is nothing to stop.")
               continue
            stopping=True
