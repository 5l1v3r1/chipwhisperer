#
# Copyright (c) 2014-2018, NewAE Technology Inc
# All rights reserved.
#
# Find this and more at newae.com - this file is part of the chipwhisperer
# project, http://www.chipwhisperer.com
#
#    This file is part of chipwhisperer.
#
#    chipwhisperer is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    chipwhisperer is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Lesser General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with chipwhisperer.  If not, see <http://www.gnu.org/licenses/>.
# ChipWhisperer is a trademark of NewAE Technology Inc., registered in the
# United States of America, the European Union, and other jurisdictions.
# ==========================================================================
import logging
import warnings
import time
import math
from threading import Condition, Thread
import struct
import pickle
import traceback
import os
import usb1

import usb.core
import usb.util

from chipwhisperer.hardware.firmware import cwlite as fw_cwlite
from chipwhisperer.hardware.firmware import cw1200 as fw_cw1200
from chipwhisperer.hardware.firmware import cw305  as fw_cw305



def packuint32(data):
    """Converts a 32-bit integer into format expected by USB firmware"""

    data = int(data)
    return [data & 0xff, (data >> 8) & 0xff, (data >> 16) & 0xff, (data >> 24) & 0xff]

def unpackuint32(buf):
    """"Converts an array into a 32-bit integer"""

    pint = buf[0]
    pint |= buf[1] << 8
    pint |= buf[2] << 16
    pint |= buf[3] << 24
    return pint

def packuint16(data):
    """Converts a 16-bit integer into format expected by USB firmware"""

    data = int(data)

    return [data & 0xff, (data >> 8) & 0xff, (data >> 16) & 0xff, (data >> 24) & 0xff]


#List of all NewAE PID's
NEWAE_VID = 0x2B3E
NEWAE_PIDS = {
    0xACE2: {'name': "ChipWhisperer-Lite",     'fwver': fw_cwlite.fwver},
    0xACE3: {'name': "ChipWhisperer-CW1200",   'fwver': fw_cw1200.fwver},
    0xC305: {'name': "CW305 Artix FPGA Board", 'fwver': fw_cw305.fwver},
}

class NAEUSB_Backend:
    """
    This backend actually talks to the USB device itself. It is designed to mostly be used via the serializer, but
    can be called directly too.
    """

    CMD_READMEM_BULK = 0x10
    CMD_WRITEMEM_BULK = 0x11
    CMD_READMEM_CTRL = 0x12
    CMD_WRITEMEM_CTRL = 0x13
    CMD_MEMSTREAM = 0x14

    def __init__(self):
        self._usbdev = None
        self._timeout = 500

    def usbdev(self):
        """Safely get USB device, throwing error if not connected"""

        if not self._usbdev: raise OSError("USB Device not found. Did you connect it first?")
        return self._usbdev

    def txrx(self, tx=[]):
        """
        Process USB command, and returns a result such as data or an encoded exception. Excepts that happen
        are not raised, but instead only printed (for debug) and passed back.
        """

        response = None

        #Get command
        cmd = tx[0]
        pickle_len = struct.unpack("!I", tx[1:5])[0]

        if pickle_len > 0:
            pickle_data = tx[5:]

            if len(pickle_data) != pickle_len:
                raise ValueError("Pickle smells funny. Check best before date.")

            payload = pickle.loads(pickle_data)
        else:
            payload = None

        if cmd == self.READ_CTRL:
            #response = self.usbdev().ctrl_transfer(payload[0], payload[1], payload[2], payload[3], payload[4], timeout=self._timeout)
            response = self.handle.controlRead(payload[0], payload[1], payload[2], payload[3], payload[4], timeout=self._timeout)
        elif cmd == self.WRITE_CTRL:
            if payload[4] != len(payload[5:]):
                raise ValueError("Specified payload length & actual do not match")
            #self.usbdev().ctrl_transfer(payload[0], payload[1], payload[2], payload[3], payload[5:], timeout=self._timeout)
            self.usbdev().controlWrite(payload[0], payload[1], payload[2], payload[3], payload[5:], timeout=self._timeout)
        elif cmd == self.CMD_READ_MEM:
            addr = payload[0]
            dlen = payload[1]
            response = self.cmdReadMem(addr, dlen)
        elif cmd == self.CMD_WRITE_MEM:
            addr = payload[0]
            data = payload[1:]
            self.cmdWriteMem(addr, data)
        elif cmd == self.GET_POSSIBLE_DEVICES:
            response = self.get_possible_devices(payload)
        elif cmd == self.OPEN:
            response = self.open(serial_number=payload)
        elif cmd == self.CLOSE:
            self.close()
        elif cmd == self.WRITE_BULK:
            self.cmdWriteBulk(payload)
        elif cmd == self.FLUSH_INPUT:
            self.flushInput()
        elif cmd == self.READ:
            dlen = payload[0]
            response = self.read(dlen)
        else:
            raise ValueError("Unknown Command: %02x"%cmd)

    def is_accessable(self, dev):
        try:
            dev.getSerialNumber()
            return True
        except:
            return False

    def open(self, serial_number=None, idProduct=None, connect_to_first=False):
        """
        Connect to device using default VID/PID
        """
        inaccessable_in_devlist = False
        dev_list = self.get_possible_devices(idProduct, serial_number)
        naelist = self.get_naelist(dev_list)
        naelist_accessable = [dev for dev in naelist if self.is_accessable(dev)]
        if len(naelist) == 0:
            add_info = ""
            if serial_number:
                add_info = " with serial number {}".format(serial_number)
            raise OSError("Could not find ChipWhisperer{}. Is it connected?".format(add_info))

        if len(naelist_accessable) == 0:
            logging.error("Found ChipWhisperer, but device not accessable")
            logging.error("Try checking that you have permission to access the device and that it isn't being used elsewhere (i.e. in another Python instance, or in this script)")
            self.handle = naelist[0].open() #should throw error, if not, it's fine I guess

        if len(naelist_accessable) > 1:
            sns = ["{}:{}".format(dev.getProduct(), dev.getSerialNumber()) for dev in naelist_accessable]
            raise Warning("Multiple ChipWhisperers connected, please specify serial number." \
                          "\nDevices:\n \
                          {}".format(sns))
        self.device = naelist_accessable[0]
        try:
            self.handle = self.device.open()
        except usb1.USBError as e:
            logging.error("Could not open USB device.")
            if e.value == -3:
                logging.error("Check that the ChipWhisperer is not already connected")
                logging.error("Or that you have the proper permissions to access it")
        self._usbdev = self.handle
        self.handle.claimInterface(0)

        self.sn = self.handle.getSerialNumber()
        logging.info('Found %s, Serial Number = %s' % (self.handle.getProduct(), self.sn))

        self.rep = 0x81
        self.wep = 0x02
        self._timeout = 200

        return self.handle

    def close(self):
        """Close the USB connection"""
        try:
            del self.handle
            del self.device
        except:
            logging.info('USB Failure calling dispose_resources: %s' % str(e))

    def get_naelist(self, dev_list):
        return [dev for dev in dev_list if dev.getVendorID() == 0x2b3e]

    def get_possible_devices(self, idProduct=None, sn=None, dictonly=True):
        """Get list of USB devices that match NewAE vendor ID (0x2b3e) and
        optionally a product ID

        Checks VendorID, then

        Args:
            idProduct (list of int, optional): If not None, the product ID to match
            sn (string, optional): If not None,

        Returns:
            List of USBDevice that match Vendor/Product IDs
            """
        def get_prodlist(dev_list):
            if not idProduct: #skip if product ID not defined
                return dev_list
            return [dev for dev in dev_list if dev.getProductID() in idProduct]

        def get_snlist(dev_list):
            if not sn:
                return dev_list
            ret = []
            for dev in dev_list:
                try:
                    if dev.getSerialNumber() == sn:
                        ret.append(dev)
                except usb1.USBErrorAccess:
                    continue
            return ret
        self.usb_ctx = usb1.USBContext()
        dev_list = self.usb_ctx.getDeviceList(skip_on_error=True, skip_on_access_error=True)
        return get_snlist(get_prodlist(self.get_naelist(dev_list)))


    def sendCtrl(self, cmd, value=0, data=[]):
        """
        Send data over control endpoint
        """
        # Vendor-specific, OUT, interface control transfer
        return self.handle.controlWrite(0x41, cmd, value, 0, data, timeout=self._timeout)
        #return self.usbdev().ctrl_transfer(0x41, cmd, value, 0, data, timeout=self._timeout)

    def readCtrl(self, cmd, value=0, dlen=0):
        """
        Read data from control endpoint
        """
        # Vendor-specific, IN, interface control transfer
        return self.handle.controlRead(0xC1, cmd, value, 0, dlen, timeout=self._timeout)
        #return self.usbdev().ctrl_transfer(0xC1, cmd, value, 0, dlen, timeout=self._timeout)


    def cmdReadMem(self, addr, dlen):
        """
        Send command to read over external memory interface from FPGA. Automatically
        decides to use control-transfer or bulk-endpoint transfer based on data length.
        """

        dlen = int(dlen)

        if dlen < 48:
            cmd = self.CMD_READMEM_CTRL
        else:
            cmd = self.CMD_READMEM_BULK

        # ADDR/LEN written LSB first
        pload = packuint32(dlen)
        pload.extend(packuint32(addr))
        self.sendCtrl(cmd, data=pload)

        # Get data
        if cmd == self.CMD_READMEM_BULK:
            data = self.handle.bulkRead(self.rep, dlen, timeout=self._timeout)
            #data = self.usbdev().read(self.rep, dlen, timeout=self._timeout)
        else:
            data = self.readCtrl(cmd, dlen=dlen)

        return data

    def cmdWriteMem(self, addr, data):
        """
        Send command to write memory over external memory interface to FPGA. Automatically
        decides to use control-transfer or bulk-endpoint transfer based on data length.
        """

        dlen = len(data)

        if dlen < 48:
            cmd = self.CMD_WRITEMEM_CTRL
        else:
            cmd = self.CMD_WRITEMEM_BULK

        # ADDR/LEN written LSB first
        pload = packuint32(dlen)
        pload.extend(packuint32(addr))

        if cmd == self.CMD_WRITEMEM_CTRL:
            pload.extend(data)

        self.sendCtrl(cmd, data=pload)


        # Get data
        if cmd == self.CMD_WRITEMEM_BULK:
            data = self.handle.bulkWrite(self.wep, data, timeout=self._timeout)
            #data = self.usbdev().write(self.wep, data, timeout=self._timeout)
        else:
            #logging.warning("Write ignored")

            pass

        return data

    def cmdWriteBulk(self, data):
        """
        Write data directly to the bulk endpoint.
        :param data: Data to be written
        :return:
        """
        self.handle.bulkWrite(self.wep, data, timeout=self._timeout)
        #self.usbdev().write(self.wep, data, timeout=self._timeout)

    writeBulk = cmdWriteBulk


    def flushInput(self):
        """Dump all the crap left over"""
        try:
            # TODO: This probably isn't needed, and causes slow-downs on Mac OS X.
            #self.usbdev().read(self.rep, 1000, timeout=0.010)
            self.handle.bulkRead(self.rep, 1000, timeout=0.010)
        except:
            pass

    def read(self, dbuf, timeout):
        return self.handle.bulkRead(self.rep, dbuf, timeout)
        #return self.usbdev().read(self.rep, dbuf, timeout)


class NAEUSB(object):
    """
    USB Interface for NewAE Products with Custom USB Firmware. This function allows use of a daemon backend, as it is
    not directly touching the USB device itself.
    """

    CMD_FW_VERSION = 0x17

    CMD_READMEM_BULK = 0x10
    CMD_WRITEMEM_BULK = 0x11
    CMD_READMEM_CTRL = 0x12
    CMD_WRITEMEM_CTRL = 0x13
    CMD_MEMSTREAM = 0x14

    stream = False

    # TODO: make this better
    fwversion_latest = [0, 11]
    def __init__(self):
        self._usbdev = None
        self.handle=None
        self.usbtx = NAEUSB_Backend()
        self.usbseralizer = self.usbtx

    def get_possible_devices(self, idProduct):
        return self.usbseralizer.get_possible_devices(idProduct)

    #unfortunate hack
    def write(self):
        return self.usbtx.handle.write(self.wep, data, timeout=usbtx.handle)

    def con(self, idProduct=[0xACE2], connect_to_first=False, serial_number=None):
        """
        Connect to device using default VID/PID
        """

        self.usbseralizer.open(idProduct=idProduct, serial_number=serial_number)


        self.snum=self.usbtx.sn
        fwver = self.readFwVersion()
        logging.info('SAM3U Firmware version = %d.%d b%d' % (fwver[0], fwver[1], fwver[2]))

        return True

    def usbdev(self):
        raise AttributeError("Do Not Call Me")

    def close(self):
        """Close USB connection."""
        self.usbseralizer.close()
        self.snum = None

    def readFwVersion(self):
        try:
            data = self.readCtrl(self.CMD_FW_VERSION, dlen=3)
            return data
        except usb.USBError:
            return [0, 0, 0]

    def sendCtrl(self, cmd, value=0, data=[]):
        """
        Send data over control endpoint
        """
        # Vendor-specific, OUT, interface control transfer
        self.usbseralizer.sendCtrl(cmd, value, data)

    def readCtrl(self, cmd, value=0, dlen=0):
        """
        Read data from control endpoint
        """
        # Vendor-specific, IN, interface control transfer
        return self.usbseralizer.readCtrl(cmd, value, dlen)

    def cmdReadMem(self, addr, dlen):
        """
        Send command to read over external memory interface from FPGA. Automatically
        decides to use control-transfer or bulk-endpoint transfer based on data length.
        """

        return self.usbseralizer.cmdReadMem(addr, dlen)

    def cmdWriteMem(self, addr, data):
        """
        Send command to write memory over external memory interface to FPGA. Automatically
        decides to use control-transfer or bulk-endpoint transfer based on data length.
        """

        return self.usbseralizer.cmdWriteMem(addr, data)

    def writeBulkEP(self, data):
        """
        Write directoly to the bulk endpoint.
        :param data: Data to be written.
        :return:
        """

        return self.usbseralizer.writeBulk(data)

    def flushInput(self):
        """Dump all the crap left over"""
        self.usbseralizer.flushInput()

    def cmdReadStream_getStatus(self):
        """
        Gets the status of the streaming mode capture, tells you samples left to stream out along
        with overflow buffer status. When an overflow occurs the samples left to stream goes to
        zero.

        samples_left_to_stream is number of samples not yet streamed out of buffer.
        overflow_lcoation is the value of samples_left_to_stream when a buffer overflow occured.
        unknown_overflow is a flag indicating if an overflow occured at an unknown time.

        Returns:
            Tuple indicating (samples_left_to_stream, overflow_location, unknown_overflow)
        """
        data = self.readCtrl(self.CMD_MEMSTREAM, dlen=9)

        status = data[0]
        samples_left_to_stream = unpackuint32(data[1:5])
        overflow_location = unpackuint32(data[5:9])

        if status == 0:
            unknown_overflow = False
        else:
            unknown_overflow = True

        return (samples_left_to_stream, overflow_location, unknown_overflow)

    def cmdReadStream_size_of_fpgablock(self):
        """ Asks the hardware how many BYTES are read in one go from FPGA, which indicates where the sync
            bytes will be located. These sync bytes must be removed in post-processing. """
        return 4096

    def cmdReadStream_bufferSize(self, dlen):
        """
        Args:
            dlen: Number of samples to be requested (will be rounded to something else)

        Returns:
            Tuple: (Size of temporary buffer required, actual samples in buffer)
        """
        num_samplebytes = int(math.ceil(float(dlen) * 4 / 3))
        num_blocks = int(math.ceil(float(num_samplebytes) / 4096))
        num_totalbytes = num_samplebytes + num_blocks
        num_totalbytes = int(math.ceil(float(num_totalbytes) / 4096) * 4096)
        return (num_totalbytes, num_samplebytes)


    def initStreamModeCapture(self, dlen, dbuf_temp, timeout_ms=1000):
        #Enter streaming mode for requested number of samples
        if hasattr(self, "streamModeCaptureStream"):
            self.streamModeCaptureStream.join()
        self.sendCtrl(NAEUSB.CMD_MEMSTREAM, data=packuint32(dlen))
        self.streamModeCaptureStream = NAEUSB.StreamModeCaptureThread(self, dlen, dbuf_temp, timeout_ms)
        self.streamModeCaptureStream.start()

    def cmdReadStream_isDone(self):
        return self.streamModeCaptureStream.isAlive() == False

    def cmdReadStream(self):
        """
        Gets data acquired in streaming mode.
        initStreamModeCapture should be called first in order to make it work.
        """
        self.streamModeCaptureStream.join()

        # Flush input buffers in case anything was left
        try:
            #self.cmdReadMem(self.rep)
            self.usbtx.read(4096, timeout=10)
            self.usbtx.read(4096, timeout=10)
            self.usbtx.read(4096, timeout=10)
            self.usbtx.read(4096, timeout=10)
        except IOError:
            pass

        # Ensure stream mode disabled
        self.sendCtrl(NAEUSB.CMD_MEMSTREAM, data=packuint32(0))

        return self.streamModeCaptureStream.drx, self.streamModeCaptureStream.timeout

    def enterBootloader(self, forreal=False):
        """Erase the SAM3U contents, forcing bootloader mode. Does not screw around."""

        if forreal:
            self.sendCtrl(0x22, 3)

    def read(self, dlen, timeout=2000):
        self.usbserializer.read(dlen, timeout)

    class StreamModeCaptureThread(Thread):
        def __init__(self, serial, dlen, dbuf_temp, timeout_ms=2000):
            """
            Reads from the FIFO in streaming mode. Requires the FPGA to be previously configured into
            streaming mode and then arm'd, otherwise this may return incorrect information.

            Args:
                dlen: Number of samples to request.
                dbuf_temp: Temporary data buffer, must be of size cmdReadStream_bufferSize(dlen) or bad things happen
                timeout_ms: Timeout in ms to wait for stream to start, otherwise returns a zero-length buffer
            Returns:
                Tuple of (samples_per_block, total_bytes_rx)
            """
            Thread.__init__(self)
            self.dlen = dlen
            self.dbuf_temp = dbuf_temp
            self.timeout_ms = timeout_ms
            self.serial = serial
            self.timeout = False
            self.drx = 0

        def run(self):
            logging.debug("Streaming: starting USB read")
            start = time.time()
            try:
                self.drx = self.serial.usbtx.read(self.dbuf_temp, timeout=self.timeout_ms)
            except IOError as e:
                logging.warning('Streaming: USB stream read timed out')
            diff = time.time() - start
            logging.debug("Streaming: Received %d bytes in time %.20f)" % (self.drx, diff))


if __name__ == '__main__':
    from chipwhisperer.hardware.naeusb.fpga import FPGA
    from chipwhisperer.hardware.naeusb.programmer_avr import AVRISP
    from chipwhisperer.hardware.naeusb.programmer_xmega import XMEGAPDI, supported_xmega
    from chipwhisperer.hardware.naeusb.serial import USART

    cwtestusb = NAEUSB()
    cwtestusb.con()

    #Connect required modules up here
    fpga = FPGA(cwtestusb)
    xmega = XMEGAPDI(cwtestusb)
    avr = AVRISP(cwtestusb)
    usart = USART(cwtestusb)

    force = True
    if fpga.isFPGAProgrammed() == False or force:
        from datetime import datetime
        starttime = datetime.now()
        fpga.FPGAProgram(open(r"C:\E\Documents\academic\sidechannel\chipwhisperer\hardware\capture\chipwhisperer-lite\hdl\cwlite_ise\cwlite_interface.bit", "rb"))
        # fpga.FPGAProgram(open(r"C:\Users\colin\dropbox\engineering\git_repos\CW305_ArtixTarget\temp\artix7test\artix7test.runs\impl_1\cw305_top.bit", "rb"))
        # fpga.FPGAProgram(open(r"C:\E\Documents\academic\sidechannel\chipwhisperer\hardware\api\chipwhisperer-lite\hdl\cwlite_ise_spifake\cwlite_interface.bit", "rb"))
        stoptime = datetime.now()
        print("FPGA Config time: %s" % str(stoptime - starttime))

    # print fpga.cmdReadMem(10, 6)
    # print fpga.cmdReadMem(0x1A, 4)
    # fpga.cmdWriteMem(0x1A, [235, 126, 5, 4])
    # print fpga.cmdReadMem(0x1A, 4)

    avrprogram = False
    if avrprogram:
        avr.enableISP(True)
        avr.enableISP(False)

    xmegaprogram = True
    if xmegaprogram:
        xmega.setChip(supported_xmega[0])
        # Worst-case is 75mS for chip erase, so give us some head-room
        xmega.setParamTimeout(200)

        try:
            print("Enable")
            xmega.enablePDI(True)

            print("Read sig")
            # Read signature bytes
            data = xmega.readMemory(0x01000090, 3, "signature")

            print(data)

            if data[0] != 0x1E or data[1] != 0x97 or data[2] != 0x46:
                print("Signature bytes failed: %02x %02x %02x != 1E 97 46" % (data[0], data[1], data[2]))
            else:
                print("Detected XMEGA128A4U")

            print("Erasing")
            # Chip erase
            try:
                xmega.eraseChip()
            except IOError:
                xmega.enablePDI(False)
                xmega.enablePDI(True)

            fakedata = [i & 0xff for i in range(0, 2048)]
            print("Programming FLASH Memory")
            xmega.writeMemory(0x0800000, fakedata, memname="flash")

            print("Verifying")
            test = xmega.readMemory(0x0800000, 512)

            print(test)


        except TypeError as e:
            print(str(e))

        except IOError as e:
            print(str(e))

        xmega.enablePDI(False)

    print("Let's Rock and Roll baby")

    sertest = True

    if sertest:
        usart.init()
        usart.write("hello\n")
        time.sleep(0.1)
        print(usart.read())
