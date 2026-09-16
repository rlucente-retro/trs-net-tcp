import time
import glob
import sys
import os
import serial
import serial.tools.list_ports
import struct

def scroll_away():
   i=0
   while (i<25):
      print("")
      i=i+1
   
def cls():
   os.system('cls' if os.name=="nt" else 'clear')
 
def stats():
   cls()
   print ("          ---->   TRS-NET   <----")
   print ("               NETWORK  SERVER")
   print ("")
   print (" Copyright 2024 - 2025 Daniel Paul Martin")
   print (" All rights reserved.")
   print (" Free to copy & distribute provided this notice is displayed")
   print (" www.danielpaulmartin.com    www.TRSDOS.com")
   print ("")
   print ("")
   print ("    S E R V E R    S T A T I S T I C S")
   print ("    __________________________________")
   print ("")
   print ("    Port " + serialPort.portstr,"@ ",danny_baud," open for network communications.",sep="")
   print ("    Server volume: DESKTOP\\TRSDOS\\"+vol0.name)
   print ("    Print SPOOL file: DESKTOP\\TRSDOS\\"+prtpath+prtname)
   print ("    Vol size =",vol0_size,"Records =",int(vol0_records)) 
   print ("    Closed status:", vol0.closed)
   print ("    Open mode:", vol0.mode)
   print ("")
   print (" Server received ",serialString,command,sep="")
   print ("")
   print ("        BIND=",bind)
   print ("        CTRL=",control)
   print ("        ECHO=",echos)
   print ("        PING=",pings)
   print ("        GETS=",gets, "retries =",rd_retry)
   print ("        PRNT=",prints)
   print ("        PUTS=",puts, "retries =",wr_retry)
   print ("        RHDR=",rhdr)
   print ("        STAT=",stat)
   print ("")
    
volpath = "volumes\\"
volname = "sys720k.dsk"

prtpath = "printer\\"
prtname = "print_out.txt"

cls
print("")
print ("          ---->   TRS-NET   <----")
print ("               NETWORK  SERVER")
print ("")
print (" Copyright 2024 - 2025 Daniel Paul Martin")
print (" All rights reserved.")
print (" But free to copy & distribute provided this notice is displayed")
print (" www.danielpaulmartin.com    www.TRSDOS.com")
print("")

volpath = str(input("Enter path to data files or enter for default=volumes\\: "))
if volpath == "":
   volpath = "volumes\\"
   
volname = str(input("Enter server data file or enter for default=sys720k.dsk: "))
if volname == "":
   volname = "sys720k.dsk"
   
vol0 = open(volpath + volname, "rb+")

serialString=""
rec_size=256
vol0.seek(0, os.SEEK_END)
vol0_size = vol0.tell()
vol0_records = vol0_size/rec_size

bind=0
control=0
echos=0
pings=0
prints=0
rhdr=0
stat=0
echo_packet_len=0
sector=0
rsector=0

gets=0
rd_retry=0

puts=0
wr_retry=0

command=""
y=0
danny_baud=""


baud_rates = ["2400", "4800", "9600", "14400", "19200", "38400","57600","76800","115200","230400","460800","576000","921600"]
y=len(baud_rates)

cls()
print ("")
print ("Select Baud Rate for network connection.")
print("")

for x in range(y):
    print (x+1,baud_rates[x],"")
    
print("")

rate =input("Baud rate or enter for default:")
if rate == "":
   rate = 9 # 115KB default baud rate.
rate=int(rate)

danny_baud=baud_rates[rate-1]
ports = serial.tools.list_ports.comports()
y=0

for port, desc, hwid in sorted(ports):
    y=y+1
    
cls()
print ("                 Welcome to server for TRSDOS.")
print ("") 
print ("")
print (" Select port for connection to TRSDOS platform.")
print ("")

for x in range(y):
    print(x+1,'-',ports[x])
    
for x in range(4):
    print ("")

print("")
s=""
while(s==""):
   s=input("Port to use? ")
s = int(s) - 1

danny =str(ports[s])
danny_port=danny[:5]

serialPort=serial.Serial(
    danny_port,
    baudrate=danny_baud,
    rtscts=True,
    bytesize=serial.EIGHTBITS,
    timeout=1,
    stopbits=serial.STOPBITS_ONE)

scroll_away()

stats()

for x in range(4):
    print ("")


while True:

      if(serialPort.in_waiting>0):
         serialString=serialPort.read().decode('utf-8')

         if (serialString == "#"): # Client is sending byte to print.

            # Here we process data being sent to TRSDOS printer. Reroute data to file here on server.
            
            prints = prints + 1
            control = control + 1

            command_data = serialPort.readline().strip()
            command_type = str(command_data)
            command_type = command_type.strip()
            command = command_type[2:7]

            vol1 = open(prtpath + prtname, "a")

            print_byte=int(command)       

            print(chr(print_byte),end="",file=vol1)

            vol1.close()
                        
         if (serialString == "<"):
             ## Here we process "<" or "\" read or reread request's.
           
            gets=gets+1

            command_data = serialPort.readline().strip()            
            command_type = str(command_data)
            command_type = command_type.strip()
            command = command_type[2:7]
           
            rsector=int(command)
               
            position = vol0.seek(((rsector*rec_size)+256),0); # Position to record in file & read one record.

            rd_sector = vol0.read(rec_size)# Read sector from file...here...

            checksum = int(sum(rd_sector) % 256)
            
            byte_data = struct.pack('>i', checksum)
            serialPort.write(rd_sector) # Send sector to client.#
            serialPort.write(byte_data) # Send checksum to client.#

         if (serialString == "\\"):
             
           # Here we process "\" reread request's.
           
            rd_retry = rd_retry+1
             
            command_data = serialPort.readline().strip()            
            command_type = str(command_data)
            command_type = command_type.strip()
            command = command_type[2:7]
            rsector=int(command)
               
            position = vol0.seek(((rsector*rec_size)+256),0); # Position to record in file & read one record.

            rd_sector = vol0.read(rec_size)# Read sector from file...here...

            checksum = int(sum(rd_sector) % 256)
            
            byte_data = struct.pack('>i', checksum)
            serialPort.write(rd_sector) # Send sector to client.#
            serialPort.write(byte_data) # Send checksum to client.#


         if (serialString == ">"):

            # Here we process ">" or WRITE requests.

            puts=puts+1

            command_data = serialPort.readline().strip()            
            command_type = str(command_data)
            command_type = command_type.strip()
            command = command_type[2:7]
           
            rsector=int(command)
            position = vol0.seek(((rsector*rec_size)+256),0); # Position to record in file.

            sector_data = serialPort.read(256) # Read buffer.
            checksum = int(sum(sector_data) % 256) # Calculate 8 bit checksum for data in sector_data.
            byte_data = struct.pack('>i', checksum) # Convert to format we can write back.
            
            chksum = serialPort.read(4) # Read checksum from serial port as calculated by TRSDOS.

            serialPort.write(byte_data) # Send checksum we calculated on buffer back to client.#
            
            # if checksum == chksum:
            vol0.write(sector_data)
            
         if (serialString == "/"): # Here we process ">" or WRITE requests.
            
            # Section of code below does rewrite. Signal code to server was /.

            wr_retry=wr_retry+1

            command_data = serialPort.readline().strip()            
            command_type = str(command_data)
            command_type = command_type.strip()
            command = command_type[2:7]
           
            rsector=int(command)
            position = vol0.seek(((rsector*rec_size)+256),0); # Position to record in file.

            sector_data = serialPort.read(256) # Read buffer.
            checksum = int(sum(sector_data) % 256) # Calculate 8 bit checksum for data in sector_data.
            byte_data = struct.pack('>i', checksum) # Convert to format we can write back.
            
            chksum = serialPort.read(4) # Read checksum from serial port as calculated by TRSDOS.

            serialPort.write(byte_data) # Send checksum we calculated on buffer back to client.#
            # print ("Put #:",puts,"TRSDOS CKSUM =:",chksum,"Python CKSUM =:",checksum,"Return sum =:",byte_data)
            
            if checksum == chksum1:
               vol0.write(sector_data)
            
                           
         if (serialString == "@" ): # Here we process @ control requests.
            command_data = serialPort.readline().strip()            
            command_type = str(command_data)
            command_type = command_type.strip()
            command = command_type[2:6]

            if (command == "stat"): # @stat Client wants server stats displayed on server.
                   
              control=control+1
              stat=stat+1
              stats()
              
            if (command == "bind"): # @bind Client sends @bind commanding server to bind volume to server.
     
              control=control+1
              bind=bind + 1
              serialPort.write(b"@bound\n") # Ping back server using @pong.

              position = vol0.seek(((0)*rec_size), 0); # Position to record in file & read one record.
              sector_data = vol0.read(rec_size) # Read record from file.

              serialPort.write(sector_data) # Echo buffer to client.
              stats()

            if (command == "ping"): # @ping Client sends @ping to test if server up and running.
     
              control=control+1
              pings = pings +1

              serialPort.write(b"@pong\n") # Ping back server using @pong.

              stats()

            if (command == "rset"): # Tell server to do a reset.
              
              control=control+1
              rset=rset+1 # @rset Tell server to reset & return @pong.
              
              stats()

            if (command == "rhdr"): # Server read header of volume.

              rhdr = rhdr + 1 # Inc counter.     
              control=control+1
              
              position = vol0.seek(0,0); # Position to record in file & read one record.
              sector_data = vol0.read(rec_size) # Read record from file.
              
              serialPort.write(sector_data) # Echo buffer to client.
              
              sector_data = serialPort.readline().strip()  
              sector_char = str(sector_data) # Convert to string.
              rsector = sector_char[3:8] # Trim off extra.
              rsector=int(rsector)
              
              position = vol0.seek(((rsector*rec_size)+256),0); # Position to record in file & read one record.
              rd_sector = vol0.read(rec_size)# Read sector from file...here...
              serialPort.write(rd_sector) # Send sector to client.
              
              sector_data = serialPort.readline().strip()  
              sector_char = str(sector_data) # Convert to string.
              
              rsector = sector_char[3:8] # Trim off extra.
              rsector=int(rsector)
              
              position = vol0.seek(((rsector*rec_size)+256),0); # Position to record in file & read one record.
              rd_sector = vol0.read(rec_size)# Read sector from file...here...
              serialPort.write(rd_sector) # Send sector to client.

              stats()

            if (command == "echo"): # Server received 256 byte packet & sends this same packet back.

              echos = echos + 1     
              control=control+1
                       
              echo_data = serialPort.read(256) # Read buffer to echo.
              echo_packet_len =len(echo_data)         

              serialPort.write(echo_data) # Echo buffer to client.
              
              stats()
