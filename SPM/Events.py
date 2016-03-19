from . import __version__, _msg_size, _hash_rounds, _data_size
from SPM.Util import log

from time import time as time_sec
from time import sleep
import os
import inspect
import socket
import hashlib

from SPM.Messages import MessageStrategy, MessageClass, MessageType
from SPM.Messages import BadMessageError
from SPM.Database import DatabaseError
from SPM.Tickets import Ticket, BadTicketError
from SPM.Stream import RC4, make_hmacf

strategies = MessageStrategy.strategies
db = None #Worker thread must initialize this to Database() because
          #sqlite3 demands same thread uses this reference

#Events and shared event data

class ClientData:
  
  def __init__(self,socket):
    self.socket = socket          #Client communication socket
    self.msg_dict = None          #Client msg about to be dispatched
    self.buf = bytearray()        #Recv buffer for client messages
    self.subject = None           #Authenticated subject under which client acts
    self.subject1 = None          #Multi-subject
    self.subject2 = None          #Multi-subject
    self.target = None            #Single subject argument
    self.t_password = None        #Target subject's password
    self.ticket = None            #Ticket
    self.salt = None              #Salt
    self.key = None               #Encryption key
    self.hmacf = None             #Message signing function
    self.stream = None            #Keystream generator object
    self.lastreadtime = None      #Last attempt to read from socket
    self.blocktime = 0            #Time to sleep after reading empty socket
    self.filename = None          #File name
    self.fd = None                #Open file discriptor
    self.in_data = None           #Incoming data block
    self.out_data = None          #Outgoing data block
    self.bytes_sent = 0           #Bytes sent of message to client
    self.curpart = 0              #Xfer progress
    self.endpart = 0              #Xfer EOF detection
    self.cd = "/"                 #Client virtual working directory
    
  def useMsg(self):
    assert self.msg_dict
    msg_dict = self.msg_dict
    self.msg_dict = None
    return msg_dict

#Utility functions (for code readability), time units are in ms
log_this_func = lambda: log(inspect.stack()[1][3])
time = lambda: time_sec()*1000

#Set first lastreadtime to a resonable value
def init_lastreadtime(scope):
  if scope.lastreadtime is None:
    scope.lastreadtime = time()

#Measure time we last tried to read from the socket and block if no data to keep the time
#  for an event to pass through the queue near target_depth ms. If the server starts
#  getting busy, we quickly back off. Ultimately the server becomes non-blocking when
#  the queue is busy. When idle, the server slowly increments the socket block time until
#  the queue is once again target_depth ms long, spending most of its time blocked on
#  the socket, waiting for data
#
#This is done to avoid spinning on the non-blocking socket when there is nothing to do but
#  check for incoming data (in this case we will soon block a long time), while not blocking
#  when the server is fully loaded (in this case we quickly handle the check socket event
#  to move on with more pressing matters...)
def update_blocktime(scope):
  init_lastreadtime(scope)
  ct = time()
  scope.blocktime = next_block_time(scope.blocktime,ct-scope.lastreadtime)
  scope.lastreadtime = ct

def next_block_time(last_time,call_delta,target_depth=500,precision=10):
  if call_delta > target_depth:
    return last_time//2
  return last_time+precision

def scan_for_message_and_parse(scope):
  if scope.msg_dict:
    raise RuntimeError("BUG! Never scan for msg before disposal of previous")
  if len(scope.buf) >= _msg_size:
    scope.msg_dict = MessageStrategy.parse(scope.buf[0:_msg_size],stream=scope.stream,hmacf=scope.hmacf)
    scope.buf = scope.buf[_msg_size:]

class Events:

  @staticmethod
  def acceptClient(dq,socket):
    log_this_func()
    addr = socket.getpeername()
    print("Accepted connection from %s:%i" % addr)
    scope = ClientData(socket)
    dq.append(lambda: Events.readUntilMessageEnd(dq,scope,
	lambda: Events.checkHelloAndReply(dq,scope)))

  @staticmethod
  def readUntilMessageEnd(dq,scope,next):
    if scope.buf:
      try:
        scan_for_message_and_parse(scope)
      except BadMessageError:
        dq.append(lambda: Events.replyErrorMessage(dq,"BadMessageError",scope,
                                                 lambda: Events.die(dq,scope)))
        return
    if scope.msg_dict:
      log("Got full message")
      dq.append(lambda: next())
    else:
      dq.append(lambda: Events.readMessagePart(dq,scope,
	lambda: Events.readUntilMessageEnd(dq,scope,next)))

  @staticmethod
  def readMessagePart(dq,scope,next):
    assert socket
    update_blocktime(scope)
    try:
      incoming_part = bytearray(scope.socket.recv(4096))
      scope.buf.extend(incoming_part)
      sleep(scope.blocktime/1000)
    except socket.timeout:
      pass
    dq.append(lambda: next())

  @staticmethod
  def sendMessage(dq,scope,next):
    assert(scope.socket)
    assert(scope.out_data)
    scope.bytes_sent = 0
    Events.sendMessagePart(dq,scope,next)

  @staticmethod
  def sendMessagePart(dq,scope,next):
    scope.bytes_sent += scope.socket.send(scope.out_data)
    if scope.bytes_sent != len(scope.out_data):
      dq.append(lambda: Events.sendMessagePart(dq,scope,next))
    else:
      scope.bytes_sent = 0
      scope.out_data = None
      dq.append(lambda: next())
      
  @staticmethod
  def checkHelloAndReply(dq,scope):
    log_this_func()
    assert(scope.msg_dict)
    msg_dict = scope.useMsg()
    if not msg_dict["MessageType"] == MessageType.HELLO_CLIENT:
      dq.append(lambda: Events.replyErrorMessage(dq,"Expected client greeting.",scope,
                                                 lambda: Events.die(dq,scope)))
      return
    log("Client reported version: %s" % msg_dict["Version"])
    if __version__ != msg_dict["Version"]:
      dq.append(lambda: Events.replyErrorMessage(dq,"Version mismatch.",scope,
                                                 lambda: Events.die(dq,scope)))
      return
    scope.out_data = strategies[(MessageClass.PUBLIC_MSG,MessageType.HELLO_SERVER)].build([__version__])
    Events.sendMessage(dq,scope, lambda: Events.waitForNextMessage(dq,scope))

  @staticmethod
  def waitForNextMessage(dq,scope):
    while len(scope.buf) >= _msg_size or scope.msg_dict:
      try:
        if not scope.msg_dict:
          scan_for_message_and_parse(scope)
      except BadMessageError:
        dq.append(lambda: Events.replyErrorMessage(dq,"BadMessageError",scope,
                                                 lambda: Events.die(dq,scope)))
        return
      msg_dict = scope.useMsg()
      msg_type = msg_dict["MessageType"]
      log(str(msg_type))
      #Switch on all possible messages
      if msg_type == MessageType.DIE:
        scope.socket.close()
        return #No parallel dispatch
      elif msg_type == MessageType.PULL_FILE:
        scope.filename = msg_dict["File Name"]
        scope.curpart = 0
        scope.endpart = 0
        dq.append(lambda: Events.pushFile(dq,scope))
        return #No parallel dispatch
      elif msg_type == MessageType.PUSH_FILE:
        scope.filename = msg_dict["File Name"]
        scope.in_data = None
        scope.curpart = 0
        scope.endpart = msg_dict["EndPart"]
        dq.append(lambda: Events.pullFile(dq,scope))
        return #No parallel dispatch
      elif msg_type == MessageType.AUTH_SUBJECT:
        scope.target = msg_dict["Subject"]
        scope.salt = msg_dict["Salt"]
        dq.append(lambda: Events.authenticate(dq,scope))
        return
      elif msg_type == MessageType.LIST_SUBJECT_CLIENT:
        dq.append(lambda: Events.listSubjects(dq,scope))
      elif msg_type == MessageType.LIST_OBJECT_CLIENT:
        dq.append(lambda: Events.listObjects(dq,scope))
      elif msg_type == MessageType.GIVE_TICKET_SUBJECT:
        scope.target = msg_dict["Subject"]
        try:
          scope.ticket = Ticket(msg_dict["Ticket"])
        except BadTicketError:
          dq.append(lambda: Events.replyErrorMessage(dq,"BadTicketError",scope,
                                                     lambda: Events.die(dq,scope)))
          return
        dq.append(lambda: Events.giveTicket(dq,scope))
      elif msg_type == MessageType.TAKE_TICKET_SUBJECT:
        scope.target = msg_dict["Subject"]
        try:
          scope.ticket = Ticket(msg_dict["Ticket"])
        except BadTicketError:
          dq.append(lambda: Events.replyErrorMessage(dq,"BadTicketError",scope,
                                                     lambda: Events.die(dq,scope)))
          return
        dq.append(lambda: Events.takeTicket(dq,scope))
      elif msg_type == MessageType.MAKE_DIRECTORY:
        scope.target = msg_dict["Directory"]
        dq.append(lambda: Events.makeDirectory(dq.scope))
      elif msg_type == MessageType.MAKE_SUBJECT:
        scope.target = msg_dict["Subject"]
        scope.t_password = msg_dict["Password"]
        dq.append(lambda: Events.makeSubject(dq,scope))
      elif msg_type == MessageType.CD:
        scope.target = msg_dict["Path"]
        dq.append(lambda: Events.changeDirectory(dq,scope))
      elif msg_type == MessageType.MAKE_FILTER:
        scope.subject1 = msg_dict["Subject1"]
        scope.subject2 = msg_dict["Subject2"]
        try:
          scope.ticket = Ticket(msg_dict["Ticket"])
        except BadTicketError:
          dq.append(lambda: Events.replyErrorMessage(dq,"BadTicketError",scope,
                                                     lambda: Events.die(dq,scope)))
          return
        dq.append(lambda: Events.makeFilter(dq,scope))
      elif msg_type == MessageType.MAKE_LINK:
        scope.subject1 = msg_dict["Subject1"]
        scope.subject2 = msg_dict["Subject2"]
        dq.append(lambda: Events.makeLink(dq,scope))
      elif msg_type == MessageType.DELETE_FILE:
        scope.filename = msg_dict["File Name"]
        dq.append(lambda: Events.deleteFile(dq,scope))
      elif msg_type == MessageType.CLEAR_FILTERS:
        scope.target = msg_dict["Subject"]
        dq.append(lambda: Events.clearFilters(dq,scope))
      elif msg_type == MessageType.CLEAR_LINKS:
        scope.target = msg_dict["Subject"]
        dq.append(lambda: Events.clearLinks(dq,scope))
      elif msg_type == MessageType.DELETE_SUBJECT:
        scope.target = msg_dict["Subject"]
        dq.append(lambda: Events.deleteSubject(dq,scope))
      else:
         dq.append(lambda: Events.replyErrorMessage(dq,"Unexpected message type",scope,
                                                     lambda: Events.die(dq,scope)))
    dq.append(lambda: Events.readUntilMessageEnd(dq,scope,
	lambda: Events.waitForNextMessage(dq,scope)))

  @staticmethod
  def pushFile(dq,scope):
    log_this_func()
    assert scope.filename
    assert scope.curpart == 0
    assert scope.endpart == 0
    assert scope.stream
    localpath = os.path.join(scope.cd,scope.filename)
    try:
      if not db.getObject(localpath):
        dq.append(lambda: Events.replyErrorMessage(dq,"Object does not exist in database",scope,
                                                 lambda: Events.die(dq,scope)))
        return
      scope.fd = db.readObject(localpath)
    except DatabaseError as e:
      dq.append(lambda: Events.replyErrorMessage(dq,str(e),scope,
                                                     lambda: Events.die(dq,scope)))
      return
    except IOError:
      dq.append(lambda: Events.replyErrorMessage(dq,"Error reading file",scope,
                                                 lambda: Events.die(dq,scope)))
      return
    log("Opened '{}' for reading".format(localpath))
    dq.append(lambda: Events.sendFilePart(dq,scope))

  @staticmethod
  def pullFile(dq,scope):
    log_this_func()
    assert scope.filename
    assert scope.curpart
    assert scope.endpart
    assert scope.stream
    if scope.curpart != 0:
      dq.append(lambda: Events.replyErrorMessage(dq,"Push must start at zero.",scope,
                                                 lambda: Events.die(dq,scope)))
    elif scope.endpart <= 0:
      dq.append(lambda: Events.replyErrorMessage(dq,"File must have a block.",scope,
                                                 lambda: Events.die(dq,scope)))
    else:
      localpath = os.path.join(scope.cd,scope.filename)
      try:
        if db.getObject(localpath):
          dq.append(lambda: Events.replyErrorMessage(dq,"Object already exists in database",scope,
                                                 lambda: Events.die(dq,scope)))
          return
        db.insertObject(localpath)
        scope.fd = db.writeObject(localpath)
      except DatabaseError as e:
        dq.append(lambda: Events.replyErrorMessage(dq,str(e),scope,
                                                     lambda: Events.die(dq,scope)))
        return
      except IOError:
        dq.append(lambda: Events.replyErrorMessage(dq,"Error writing file",scope,
                                                     lambda: Events.die(dq,scope)))
        return
      log("Opened '{}' for writing after object insertion".format(localpath))
      dq.append(lambda: Events.pullFilePart(dq,scope))

  @staticmethod
  def pullFilePart(dq,scope):
    log_this_func()
    assert scope.filename
    assert scope.fd
    assert scope.stream
    assert scope.curpart
    assert scope.endpart
    if scope.in_data:
      try:
        scope.fd.write(scope.in_data)
      except IOError:
        dq.append(lambda: Events.replyErrorMessage(dq,"Error writing file",scope,
                                                     lambda: Events.die(dq,scope)))
      return
    else:
      dq.append(lambda: Events.readUntilMessageEnd(dq, scope,
                    lambda: Events.unpackMsgToPullFilePart(dq,scope)))
      return
    if scope.curpart == scope.endpart:
      scope.curpart = 0
      scope.endpart = 0
      scope.filename = None
      scope.fd.close()
      scope.fd = None
      scope.in_data = None
      log("All file parts have been recorded.")
      dq.append(lambda: Events.waitForNextMessage(dq,scope))
    else:
      dq.append(lambda: Events.readUntilMessageEnd(dq, scope,
                    lambda: Events.unpackMsgToPullFilePart(dq,scope)))

  @staticmethod
  def unpackMsgToPullFilePart(dq,scope):
    msg_dict = scope.useMsg()
    if msg_dict["MessageType"] != MessageType.XFER_FILE:
      dq.append(lambda: Events.replyErrorMessage(dq,"Bad message sequence",scope,
                                                     lambda: Events.die(dq,scope)))
    else:
      scope.in_data = msg_dict["Data"][0:msg_dict["BSize"]]
      next_curpart = msg_dict["CurPart"]
      if next_curpart != scope.curpart:
        dq.append(lambda: Events.replyErrorMessage(dq,"Bad message sequence",scope,
                                                     lambda: Events.die(dq,scope)))
        return
      scope.curpart += 1
      dq.append(lambda: Events.pullFilePart(dq,scope))

  @staticmethod
  def sendFilePart(dq,scope):
    log_this_func()
    assert scope.filename
    assert scope.fd
    assert scope.stream
    assert scope.curpart
    assert scope.endpart
    if scope.curpart > scope.endpart:
      scope.curpart = 0
      scope.endpart = 0
      scope.filename = None
      scope.fd.close()
      scope.fd = None
      log("All file parts have been sent.")
      dq.append(lambda: Events.waitForNextMessage(dq,scope))
    else:
      try:
        data = scope.fd.read(_data_size)
      except IOError:
        dq.append(lambda: Events.replyErrorMessage(dq,"Error reading file",scope,
                                                     lambda: Events.die(dq,scope)))
        return
      scope.out_data = strategies[(MessageClass.PRIVATE_MSG,MessageType.XFER_FILE)].build([
        data,scope.curpart,len(data)],scope.stream,scope.hmacf)
      scope.curpart += 1
      Events.sendMessage(dq,scope,lambda: Events.sendFilePart(dq,scope))

  @staticmethod
  def authenticate(dq,scope):
    assert(scope.target)
    assert(scope.salt)
    try:
      target_entry = db.getSubject(scope.target)
      if target_entry:
        scope.key = hashlib.pbkdf2_hmac("sha1",target_entry.password.encode(
          "UTF-8",errors="ignore"),scope.salt,_hash_rounds, dklen=256)
        scope.stream = RC4(scope.key)
        scope.hmacf = make_hmacf(scope.key)
        scope.out_data = strategies[(MessageClass.PRIVATE_MSG,MessageType.CONFIRM_AUTH)].build([
          target_entry.subject],scope.stream,scope.hmacf)
        scope.subject = target_entry.subject
      else:
        scope.out_data = strategies[(MessageClass.PUBLIC_MSG,MessageType.REJECT_AUTH)].build()
        scope.key = None
        scope.stream = None
        scope.hmacf = None
      Events.sendMessage(dq,scope,lambda: Events.waitForNextMessage(dq,scope))
    except DatabaseError as e:
      dq.append(lambda: Events.replyErrorMessage(dq,str(e),scope,
                                                     lambda: Events.die(dq,scope)))
    except BadMessageError:
      dq.append(lambda: Events.replyErrorMessage(dq,"BadMessageError",scope,
                                                     lambda: Events.die(dq,scope)))
      
  @staticmethod
  def replyErrorMessage(dq,message,scope,next):
    log_this_func()
    log("Sent error message: %s" % message)
    if scope.stream:
      scope.out_data = strategies[(MessageClass.PRIVATE_MSG,MessageType.ERROR_SERVER)].build(message,
                                                                      scope.stream,scope.hmacf)
    else:
      scope.out_data = strategies[(MessageClass.PUBLIC_MSG,MessageType.ERROR_SERVER)].build(message)
    Events.sendMessage(dq,scope,lambda: next())

  @staticmethod
  def die(dq,scope):
    log_this_func()
    if scope.stream:
      scope.out_data = strategies[(MessageClass.PRIVATE_MSG,MessageType.DIE)].build()
    else:
      scope.out_data = strategies[(MessageClass.PUBLIC_MSG,MessageType.DIE)].build()
    Events.sendMessage(dq,scope,lambda: scope.socket.close())

