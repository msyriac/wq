"""
    %prog [options] cluster_description_file

The description file is 

    hostname ncores mem groups

The groups are optional comma separated list.
"""
from __future__ import print_function

import socket
import yaml
import time
import copy
import sys
import os
import glob

try:
    import cPickle as pickle
except ImportError:
    import pickle

import datetime

import select

DEFAULT_HOST = ''      # Symbolic name meaning all available interfaces
DEFAULT_PORT = 51093   # Arbitrary non-privileged port
DEFAULT_MAX_BUFFSIZE = 4096

# only listen for this many seconds, then refresh the queue
DEFAULT_SOCK_TIMEOUT = 30.0
DEFAULT_WAIT_SLEEP = 10.0
DEFAULT_SPOOL_DIR = "~/wqspool/"

#PRIORITY_LIST= ['block','low','med','high']
PRIORITY_LIST= ['block','high','med','low']

# how many seconds to wait before restart
RESTART_DELAY = 60

def socket_send(conn, mess):
    """
    Send a message using a socket or connection, trying until all data is sent.

    hmm... is this going to max the cpu if we can't get through right away?
    """

    mess=bytes(mess, 'utf-8')

    reslen=len(mess)
    tnsent=conn.send(mess)
    nsent = tnsent
    while nsent < reslen:
        tnsent=conn.send(mess[nsent:])
        nsent += tnsent

def socket_recieve(conn, buffsize):
    """
    Recieve all data from a socket or connection, dealing with buffers.
    """
    tdata = conn.recv(buffsize)
    data=tdata
    while len(tdata) == buffsize:
        tdata = conn.recv(buffsize)
        data += tdata

    return data
 
class Server:
    def __init__(self, cluster_file, **keys):

        # copy the state, in case we screw up somewhere and modify the keys
        self.keys = copy.deepcopy(keys) 

        self.cluster_file = cluster_file

        # note passing on state of the system.
        self.queue = JobQueue(cluster_file, **keys)
        self.buffsize = keys.get('buffsize',DEFAULT_MAX_BUFFSIZE)

        self.verbosity = 1

    def open_socket(self):
        host = self.keys.get('host',DEFAULT_HOST)
        port = self.keys.get('port',DEFAULT_PORT)
        self.timeout = self.keys.get('timeout',DEFAULT_SOCK_TIMEOUT)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((host, port))
        #self.sock.settimeout(self.timeout)
        self.sock.setblocking(0)
        self.sock.listen(4)

    def run(self):

        do_restart=True

        while True:
            self.open_socket()
            try:
                self._run()
            except KeyboardInterrupt:
                do_restart=False
            #except:
            #    es=sys.exc_info()
            #    print('caught exception type:', es[0],'details:',es[1])
            #    print('restarting')
            finally:
                self.queue.save_users()
                print('shutdown')
                self.sock.shutdown(socket.SHUT_RDWR)
                print('close')
                self.sock.close()

            if not do_restart:
                print("    keyboard interrupt: exiting")
                break
            else:
                print("    restarting after 1 minute wait")
                time.sleep(RESTART_DELAY)

    def _run(self):
        """

        Use select to tell us when either the server socket got a request or if
        clients are ready to be read.  
        
        Multiple clients are handled at once, they just go in a queue and are
        dealt with later (this queue is not the job queue, just a simple list).
        The server is in the same queue so if it gets another request before a
        client is ready to be read, then another client will be queued for
        later processing. Note we are also listening with a backlog of 4 on
        the server socket.

        Currently the clients are *not* dealt with in parallel.  This would be
        tricky since each client request can result in a change in the queue
        state.

        """
        server=self.sock
        input=[server]
        while True:
            try:

                inputready,[],[] = select.select(input,[],[],self.timeout) 
                if len(inputready) == 0:
                    self.refresh_queue()
                    #self.cleanup_failed_sockets()
                    continue

                for sock in inputready:
                    if sock == server:
                        # the server socket got a client request
                        client, addr = server.accept()
                        print(  str(datetime.datetime.now()),'Connected by', addr )
                        # it goes in the queue
                        input.append(client) 
                    else:
                        # handle clients.
                        try:
                            client=sock
                            self.process_client_request(client)
                            client.shutdown(socket.SHUT_RDWR)
                            client.close()
                        except socket.error as e:
                            es=sys.exc_info()
                            if ('Broken pipe' in es[1] 
                                    or 'Transport endpoint' in es[1]):
                                print( 'caught exception type:',es[0], )
                                print( 'details:',es[1] )
                                print( 'ignoring' )
                        #except TypeError as e:
                        #    # this happens extremely rarely, haven't tracked it down yet
                        #    # usually it is "'str' object does not support item assignment"
                        #    print( 'warning: catching and ignoring TypeError:',str(e) )
                        finally:
                            # whatever happens we can't talk to this client any
                            # more
                            input.remove(client) 

            except socket.error as e:
                es=sys.exc_info()
                if 'Broken pipe' in es[1]:
                    # this happens sometimes when someone ctrl-c in the middle
                    # of talking with the server
                    print( 'caught exception type:', es[0],'details:',es[1] )
                    print( 'ignoring Broken pipe exception' )
                else:
                    raise e

    def process_client_request(self, client):
        """
        client is a socket

        We should be ready to recieve since we used select()
        """
        data = socket_recieve(client,self.buffsize)
        if not data:
            return

        print( str(datetime.datetime.now()),'processing client request' )
        if self.verbosity > 1:
            print( data )
        try:
            message = yaml.load(data)
        except:
            ret = {"error":"could not process YAML request: '%s'" % data}
            ret = yaml.dump(ret)
            socket_send(client, ret)
            return

        self.queue.process_message(message)
        response = self.queue.get_response()

        try:
            yaml_response = yaml.dump(response)
        except:
            errmess="server error creating YAML response; keyboard interrupt?"
            err = {"error":errmess}
            yaml_response = yaml.dump(err)

        if self.verbosity > 2:
            print( 'response:',yaml_response )
        socket_send(client, yaml_response)


    def wait_for_connection(self):
        """
        we want a chance to look for disappearing pids 
        even if we don't get a signal from any clients
        """
        while True:
            try:
                conn, addr = self.sock.accept()
                print( 'Connected by', addr )
                return conn, addr
            except socket.timeout:
                # we just reached the timeout, refresh the queue
                print( 'refreshing queue' )
                self.queue.refresh()
                if self.verbosity > 1:
                    print_stat(self.queue.cluster.status())


    def refresh_queue(self):
        print( str(datetime.datetime.now()),'refreshing queue' )
        self.queue.refresh()
        if self.verbosity > 1:
            print_stat(self.queue.cluster.status())

    def cleanup_failed_sockets(self, inputs, server):
        for sock in inputs:
            if sock != server:
                pass
                #select([sock],[],[],0)

class Node:
    def __init__ (self, line):
        ls = line.split()
        host, ncores, mem = ls[0:3]

        if len(ls) > 3:
            self.grps = ls[3].split(',')
        else:
            self.grps = []

        self.host   = host
        self.ncores = int(ncores)
        self.mem    = float(mem)
        self.used   = 0
        self.online = True

    def get_groups(self):
        return self.grps

    def set_online(self,truth_value):
        self.online=truth_value

    def reserve(self):
        self.used+=1
        if (self.used>self.ncores):
            print( "Internal error." )
            sys.exit(1)

    def unreserve(self):
        self.used-=1
        if (self.used<0):
            print( "Internal error." )
            sys.exit(1)
            
    

class Cluster:
    def __init__(self,filename):
        self.filename=filename
        self.nodes={}

        with open(filename) as fobj:
            for line in fobj:
                nd = Node(line)
                self.nodes[nd.host] = nd
    
    def reserve(self,hosts):
        for h in hosts:
            self.nodes[h].reserve()

    def unreserve(self,hosts):
        for h in hosts:
            self.nodes[h].unreserve()

    def status(self):
        res={}
        tot=0
        used=0
        use=[]
        nds=[]
        nodes=list( self.nodes.keys() )
        nodes.sort()
        for h in nodes:
            nds.append({'hostname':h,
                        'used':self.nodes[h].used,
                        'ncores':self.nodes[h].ncores,
                        'mem':self.nodes[h].mem,
                        'grps':self.nodes[h].grps,
                        'online':self.nodes[h].online})
            
            tot+=self.nodes[h].ncores
            used+=self.nodes[h].used
            if (self.nodes[h].used>0):
                use.append((h,self.nodes[h].used))  

        res['used']=used
        res['ncores']=tot
        res['nnodes']=len(self.nodes)
        res['nodes']=nds
        return res


def _get_dict_int(d, key, default):
    reason=''
    try:
        N = int(d.get(key, default))
    except:
        N = None
        reason="failed to extract int requirement '%s': %s"
        reason = reason % (key,sys.exc_info()[1])

    return N, reason

def _get_dict_float(d, key, default):
    reason=''
    try:
        f = float(d.get(key, default))
    except:
        f = None
        reason="failed to extract float requirement '%s': %s"
        reason = reason % (key,sys.exc_info()[1])
    return f, reason


class Users:
    """
    Simple encapsulation so we can easily serialize
    the users dictionary
    """
    def __init__(self):
        self.users = {}
        self.verbosity = 1
    def __contains__(self, user):
        return user in self.users

    def fromfile(self, fname):
        """
        Only user and limits are loaded.
        """
        if os.path.exists(fname):

            print( 'Loading user info from:',fname )
            with open(fname) as fobj:
                data = yaml.load(fobj)

            self.users={}
            for user,udata in data.items():
                u = self._new_user(user)
                u['limits'] = udata['limits']
                self.users[user] = u

    def tofile(self, fname):
        """
        Write to file.  Only the username and limits are saved.
        """
        data={}
        for user,udata in self.users.items():
            data[user] = {}
            data[user]['user'] = user
            data[user]['limits'] = udata['limits']

        with open(fname,'w') as fobj:
            yaml.dump(data, fobj)

    def get(self, user):
        udata = self.users.get(user,None)
        if udata is None:
            udata = self.add_new(user)
        return udata


    def add_new(self, user):
        udata = self.users.get(user,None)
        if udata is None:
            udata = self._new_user(user)
            self.users[user] = udata
        return udata

    def increment_user(self,user, hosts):
        udata = self.users.get(user,None)
        if udata is None:
            udata = self.add_new(user)

        ncores = len(hosts)
        if ncores > 0:
            udata['Njobs'] += 1
            udata['Ncores'] += ncores

    def decrement_user(self, user, hosts):
        udata = self.users.get(user,None)
        if not udata:
            return

        ncores = len(hosts)
        if ncores > 0:
            udata['Njobs'] -= 1
            udata['Ncores'] -= ncores

        if udata['Njobs'] < 0:
            udata['Njobs'] = 0
        if udata['Ncores'] < 0:
            udata['Ncores'] = 0


    def _new_user(self, user):
        return {'user':user,'Njobs':0,'Ncores':0,'limits':{}}

    def asdict(self):
        return copy.deepcopy(self.users)

class Job(dict):

    def __init__(self, message, **keys):
        # make sure pid,require are in message
        # and copy them into self

        for k in message:
            self[k] = message[k]

        if 'require' not in self:
            self['status'] = 'nevermatch'
            self['reason'] = "'require' field not in message"
        elif 'pid' not in self:
            self['status'] = 'nevermatch'
            self['reason'] = "'pid' field not in message"
        elif 'user' not in self:
            self['status'] = 'nevermatch'
            self['reason'] = "'user' field not in message"
        elif 'commandline' not in self:
            self['status'] = 'nevermatch'
            self['reason'] = "'commandline' field not in message"
        else:
            self['status'] = 'wait'
            self['reason'] = ''

        spool_dir = keys.get('spool_dir',DEFAULT_SPOOL_DIR)
        self.spool_dir = os.path.expanduser(spool_dir)

        self.wait_sleep = keys.get('wait_sleep',DEFAULT_WAIT_SLEEP)

        self['priority'] = self['require'].get('priority','med')
        if self['priority'] not in PRIORITY_LIST: 
            self['status'] = 'nevermatch'
            self['reason']="priority must be on of: " + ",".join(PRIORITY_LIST)

        self['time_sub'] = time.time()
        self['spool_fname'] = None

        self.verbosity = 1

    def spool(self):
        if self['status'] == 'ready':
            self['status'] = 'run'

        fname = os.path.join(self.spool_dir,str(self['pid'])+'.'+self['status'])
        self.unspool() ## just remove the old one first
                       ## we could just rename, but things that were waiting
                       ## maybe running now

        self['spool_fname'] = fname
        self['spool_wait'] = self.wait_sleep
        if self['status'] in ['ready','run']:
            self['time_run'] = time.time()
        else:
            self['time_run'] = None
            
        with open(fname,'wb') as fobj:
            pickle.dump(self,fobj,-1) #highest protocol

    def unspool(self):
        if (self['spool_fname']):
            if os.path.exists(self['spool_fname']):
                os.remove(self['spool_fname'])
            self['spool_fname']=None
    

    def match(self, cluster, blocked_groups):
        if self['status'] == 'nevermatch':
            return
        if self['status'] != 'wait':
            return
        ## We don't block ourserlves
        if self['priority'] == 'block':
            blocked_groups=[]
 
        # default to bycore
        submit_mode = self['require'].get('mode','bycore')
        # can also add reasons from the match methods
        # later

        if (submit_mode=='bycore'):
            pmatch, match, hosts, reason = \
                    self._match_bycore(cluster,blocked_groups)
        elif (submit_mode=='bycore1'):
            pmatch, match, hosts, reason = \
                    self._match_bycore1(cluster,blocked_groups)
        elif (submit_mode=='bynode'):
            pmatch, match, hosts, reason = \
                    self._match_bynode(cluster,blocked_groups)
        elif (submit_mode=='byhost'):
            pmatch, match, hosts,reason = \
                    self._match_byhost(cluster,blocked_groups)
        elif (submit_mode=='bygroup'):
            pmatch, match, hosts,reason = \
                    self._match_bygroup(cluster,blocked_groups)
        else:
            pmatch=False ## unknown request never mathces
            reason="bad submit_mode '%s'" % submit_mode


        if pmatch:
            if match:
                self['hosts']=hosts
                self['status']='ready'
                self['reason']=''
            else:
                self['status']='wait'
                self['reason']=reason

        else:
            self['status']='nevermatch'
            self['reason']=reason

    def match_users(self, users):
        """
        ret False if user specifications are not met

        users is just a dictionary
        """
        
        # if user is not even known, then we are good
        if self['user'] in users:
            udata = users.get(self['user'])
            # if no limits are specified, we are good
            ulimits = udata['limits']
            if ulimits:

                Njobs_max = ulimits.get('Njobs',-1)
                if Njobs_max >= 0:
                    # this is the actual number of jobs the user has
                    Njobs = udata.get('Njobs',0)
                    if Njobs >= Njobs_max:
                        return False

                Ncores_max = ulimits.get('Ncores',-1)
                if Ncores_max >= 0:
                    # this is the actual number of cores the user has
                    Ncores = udata.get('Ncores',0)
                    if Ncores >= Ncores_max:
                        return False

        return True

    def _get_req_list(self, reqs, key):
        """
        If a scalar is found, itis converted to a list using [val]
        """
        val = reqs.get(key,[])
        if not isinstance(val, list):
            val = [val]
        return val


    def _match_bycore(self, cluster, bgroups):
        pmatch=False
        match=False
        hosts=[] # actually matched hosts
        reason=''

        reqs = self['require']

        N,reason=_get_dict_int(reqs, 'N', 1)
        if reason:
            return pmatch, match, hosts, reason

        threads,reason = _get_dict_int(reqs, 'threads', 1)
        if reason:
            return pmatch, match, hosts, reason

        if (threads<1):
            threads=1

        if (N%threads>0):
            reason = 'Number of requested cores not divisible by threads'
            return pmatch, match, hosts, reason
        print( "threads,N",threads,N )
        Np=N

        min_mem, reason = _get_dict_float(reqs,'min_mem',0.0)
        if reason:
            return pmatch, match, hosts, reason

        block_flag=False
        for h in sorted(cluster.nodes):
            nd = cluster.nodes[h]
            if(not nd.online):
                continue
            if (nd.mem < min_mem):
                continue

            ## is this node actually what we want
            ing = self._get_req_list(reqs, 'group')
            if len(ing) > 0: ##any group in any group
                ok=False
                for g in ing:
                    if g in nd.grps:
                        ok=True
                        break
                if (not ok):
                    continue ### not in the group
                    
            ing = self._get_req_list(reqs, 'notgroup')
            if len(ing) > 0: ##any group in any group
                ok=True
                for g in ing:
                    if g in nd.grps:
                        ok=False
                        break
                if (not ok):
                    continue ### not in the group
                
            # usable cores must be multiple of
            # number of threads requested
            pucores = (nd.ncores//threads)*threads

            if (pucores>=Np):
                pmatch=True
            else:
                Np-=pucores

            nfree= nd.ncores-nd.used
            nfree = (nfree//threads)*threads

            if len(bgroups) > 0: ##any group in any group
                ok=True
                for g in bgroups:
                    if g in nd.grps:
                        ok=False
                        block_flag=True
                        break
                if (not ok):
                    nfree=0

            if (nfree>=N):
                hosts += [h]*N
                N=0
                match=True
                break
            else:
                N-=nfree
                hosts += [h]*nfree
 
        if (not pmatch):
            reason = 'Not enough cores or mem satistifying condition.'
        elif (not match):
            if (block_flag):
                reason = ('Not enough free cores or cores waiting '
                          'for a blocking job.')
            else:
                reason = 'Not enough free cores.'
    
        if self.verbosity > 1:
            print( pmatch, match, hosts, reason )
        return pmatch, match, hosts, reason



    def _match_bycore1(self, cluster,bgroups):
        """
        Get cores all from one node.
        """
        pmatch=False
        match=False
        hosts=[] # actually matched hosts
        reason=''

        reqs = self['require']

        N,reason=_get_dict_int(reqs, 'N', 1)
        if reason:
            return pmatch, match, hosts, reason
        Np=N

        min_mem, reason = _get_dict_float(reqs,'min_mem',0.0)
        if reason:
            return pmatch, match, hosts, reason

        block_flag=False
        for h in sorted(cluster.nodes):
            nd = cluster.nodes[h]
            if(not nd.online):
                continue
            if (nd.mem < min_mem):
                continue
            
            ## is this node actually what we want
            ing = self._get_req_list(reqs, 'group')
            if len(ing) > 0: ##any group in any group
                ok=False
                for g in ing:
                    if g in nd.grps:
                        ok=True
                        break
                if (not ok):
                    continue ### not in the group
                    
            ing = self._get_req_list(reqs, 'notgroup')
            if len(ing) > 0: ##any group in any group
                ok=True
                for g in ing:
                    if g in nd.grps:
                        ok=False
                        break
                if (not ok):
                    continue ### not in the group

            if (nd.ncores>=Np):
                pmatch=True
            else:
                pass

            nfree= nd.ncores-nd.used
            
            if len(bgroups) > 0: ##any group in any group
                ok=True
                for g in bgroups:
                    if g in nd.grps:
                        ok=False
                        block_flag=True
                        break
                if (not ok):
                    nfree=0 

            if (nfree>=N):
                hosts += [h]*N
                N=0
                match=True
                break
            else:
                pass

        if (not pmatch):
            reason = 'Not a node with that many cores.'
        elif (not match):
            if (block_flag):
                reason = ('Not enough free cores or cores waiting '
                          'for a blocking job.')
            else:
                reason = 'Not enough free cores on any one node.'
    
        return pmatch, match, hosts, reason
       

    def _match_bynode(self, cluster,bgroups):
        pmatch=False
        match=False
        hosts=[] # actually matched hosts
        reason=''

        reqs = self['require']

        N,reason=_get_dict_int(reqs, 'N', 1)
        if reason:
            return pmatch, match, hosts, reason

        Np=N

        min_mem, reason = _get_dict_float(reqs,'min_mem',0.0)
        if reason:
            return pmatch, match, hosts, reason

        min_cores,reason=_get_dict_int(reqs, 'min_cores', 0)
        if reason:
            return pmatch, match, hosts, reason

        block_flag=False
        for h in sorted(cluster.nodes):
            nd = cluster.nodes[h]
            if(not nd.online):
                continue
            if (nd.mem < min_mem):
                continue
            if nd.ncores < min_cores:
                continue

            ## is this node actually what we want
            ing = self._get_req_list(reqs, 'group')
            if len(ing) > 0: ##any group in any group
                ok=False
                for g in ing:
                    if g in nd.grps:
                        ok=True
                        break
                if (not ok):
                    continue ### not in the group

            ing = self._get_req_list(reqs, 'notgroup')
            if len(ing) > 0: ##any group in any group
                ok=True
                for g in ing:
                    if g in nd.grps:
                        ok=False
                        break
                if (not ok):
                    continue ### not in the group
            
            Np-=1
            if (Np==0):
                pmatch=True

            ok=True
            if len(bgroups) > 0: ##any group in any group ##########nfree?
                for g in bgroups:
                    if g in nd.grps:
                        ok=False
                        block_flag=True
                        break

            if (nd.used==0) and ok:
                N-=1
                hosts += [h]*nd.ncores
                if (N==0):
                    match=True
                    break

        if (not pmatch):
            reason = 'Not enough total cores satistifying condition.'
        elif (not match):
            if (block_flag):
                reason = ('Not enough free cores or cores '
                          'waiting for a blocking job.')
            else:
                reason = 'Not enough free cores.'
    

        return pmatch, match, hosts, reason

    def _match_byhost(self, cluster, bgroups):

        pmatch=False
        match=False
        hosts=[] # actually matched hosts
        reason=''

        reqs = self['require']
        
        h = reqs.get('host',None)
        if h is None:
            reason = "'host' field not in requirements"
            return pmatch, match, hosts, reason

        # make sure the node name exists
        if h not in cluster.nodes:
            reason = "host '%s' does not exist" % h
            return pmatch, match, hosts, reason

        nd = cluster.nodes[h]

        if (not nd.online):
            reason = "host is offline"
            return pmatch, match, hosts, reason

        for g in nd.grps:
            if g in bgroups:
                reason="host in blocked group"
                return pmatch, match, hosts, reason

        N,reason=_get_dict_int(reqs, 'N', 1)
        if reason:
            return pmatch, match, hosts, reason

        if nd.ncores >= N:
            pmatch=True

        nfree = nd.ncores-nd.used
        if (nfree>=N):
            hosts += [h]*N
            N=0
            match=True
        else:
            reason = "Not enough free cores on "+h

        return pmatch, match, hosts, reason


    def _match_bygroup(self, cluster, bgroups):

        pmatch=False
        match=False
        hosts=[] # actually matched hosts
        reason=''

        reqs = self['require']
        g=reqs.get('group',None)
        if not g:
            pmatch=False
            reason = 'Need to specify group'
        else:
            for h in sorted(cluster.nodes):
                nd = cluster.nodes[h]
                if(not nd.online):
                    continue
                if g in nd.grps:
                    pmatch=True
                    match=True
                    if (nd.used>0):
                        match=False ## we actually demand the entire group
                        reason = 'Host '+h+' not entirely free.'
                        break
                    ok=True
                    for g in nd.grps:
                        if g in bgroups:
                            ok=False
                            break
                    if (not ok):
                        match=False
                        reason = 'Host '+h+' in a blocked group.'
                        break

                    else:
                        hosts += [h]*nd.ncores
                        #for x in range(nd.ncores):
                        #    hosts.append(h)
            if (not pmatch):
                reason = 'Not a single node in that group'
        return pmatch, match, hosts, reason


    def asdict(self):
        d={}
        for k in self:
            d[k] = self[k]
        return d

class JobQueue:
    def __init__(self, cluster_file, **keys):

        # copy the state, in case we screw up somewhere and modify the keys
        self.keys = copy.deepcopy(keys)

        self.setup_spool()

        print( "Loading cluster from:",cluster_file )
        self.cluster = Cluster(cluster_file)
        self.queue = []

        self.load_users()
        self.load_spool()

        print_users(self.users.asdict())

        print_stat(self.cluster.status())

        self.verbosity = 1

    def setup_spool(self):
        spool_dir=self.keys.get('spool_dir',DEFAULT_SPOOL_DIR)
        self.spool_dir = os.path.expanduser(spool_dir)
        if not os.path.exists(self.spool_dir):
            print( 'making spool dir:',self.spool_dir )
            os.makedirs(self.spool_dir)

    def users_file(self):
        return os.path.join(self.spool_dir, 'users.yaml')

    def load_users(self):
        self.users = Users()
        fname = self.users_file()
        self.users.fromfile(fname)

    def save_users(self):
        fname = self.users_file()
        print( 'saving users to:',fname )
        self.users.tofile(fname)

    def load_spool(self):
        print( "Loading jobs from:",self.spool_dir )
        pattern = os.path.join(self.spool_dir,'*')
        flist = glob.glob(pattern)
        for fn in sorted(flist):
            if fn[-4:] == '.run' or fn[-5:] == '.wait':
                job = None

                with open(fn,'rb') as fobj:
                    try:
                        job = pickle.load(fobj)
                    except:
                        print( 'could not unpickle job file:',fn )
                        es=sys.exc_info()
                        print( 'caught unpickle exception:', es[0],'details:',es[1] )

                if job:
                    if job['status']=='run':
                        # here we need to reserve the cluster and increment the
                        # user data
                        self.cluster.reserve(job['hosts'])
                        self.users.increment_user(job['user'], job['hosts'])
                    self.queue.append(job)


    def process_message(self, message):
        # we will overwrite this
        self.response = copy.deepcopy(message)

        if not isinstance(message,dict):
            self.response['error'] = "message should be a dictionary"
        elif 'command' not in message:
            self.response['error'] = "message should contain a command"
        else:
            self._process_command(message)

    def refresh(self):
        """
        refresh the job list

        This is the key, as it tells the jobs when they can run.

            - Remove jobs where the pid no longer is valid.
        Otherwise run match(cluster) and
            - are the requirements met and we can run?
            - note we should have no 'nevermatch' status here

        """

        pids_to_del = []
        blocked_groups=[]
        have_blocked_groups=False
        for priority in PRIORITY_LIST:
            for job in self.queue:
                if job['priority'] != priority:
                    continue
                # job was told to run.
                # see if the pid is still running, if not remove the job
                if not self._pid_exists(job['pid']):
                    print( 'removing job %s, pid no longer valid' % job['pid'] )
                    pids_to_del.append(job['pid'])

                    self._unreserve_job_and_decrement_user(job)

                elif job['status'] != 'run':
                    if not job.match_users(self.users):
                        # blame yourself
                        job['reason'] = 'user limits exceeded'
                    else:
                        # see if we can now run the job.
                        # After all blocked jobs have been scheduled (or not)
                        # we updaate the list of blocked groups (it can't change
                        # later)
                        if (priority!='block') and (not have_blocked_groups):
                            blocked_groups=self._blocked_groups()
                            have_blocked_groups = True
                            
                        job.match(self.cluster, blocked_groups)
                        
                        if job['status'] == 'ready':
                            self.cluster.reserve(job['hosts'])
                            # this will remove any pid.wait file and write a
                            # pid.run file sets status to 'run'
                            job.spool()

                            # keep statistics for each user
                            self.users.increment_user(job['user'], job['hosts'])

        # rebuild the queue without these items
        if len(pids_to_del) > 0:
            self.queue = [j for j in self.queue if j['pid'] not in pids_to_del]

    def _unreserve_job_and_decrement_user(self, job):
        job.unspool()
        if job['status'] == 'run':
            self.users.decrement_user(job['user'], job['hosts'])
            self.cluster.unreserve(job['hosts'])
            job['status'] = 'done'


    def get_response(self):
        return self.response


    def _process_command(self, message):
        command = message['command']
        print( str(datetime.datetime.now()),'  got',command,'request' )

        if command in ['sub']:
            self._process_submit_request(message)
        elif command == 'gethosts':
            self._process_gethosts(message)
        elif command in ['ls']:
            self._process_listing_request(message)
        elif command in ['lsfull']:
            self._process_full_listing_request(message)
        elif command in ['stat']:
            self._process_status_request(message)
        elif command in ['users']:
            self._process_userlist_request()
        elif command in ['limit']:
            self._process_limit_request(message)
        elif command in ['rm']:
            self._process_remove_request(message)
        elif command == 'notify':
            self._process_notification(message)
        elif command == 'refresh':
            self.refresh()
            self.response['response'] = 'OK'
        elif command =='node':
            self._process_node_request(message)
        else:
            self.response['error'] = ("only support 'sub','gethosts', "
                                      "'ls','stat','users','rm','notify','node'"
                                      "'refresh' commands")
    def _process_node_request(self, message):

        nodename = message['node']
        if (not message['yamline'].has_key('status')):
            self.response['error']=('Need to supply status keyword.')
            return None
        
        status=message['yamline']['status']

        if (status=='online'):
            setstat=True
        elif (status=='offline'):
            setstat=False
        else:
            self.response['error']=("Don't understand this status")
            return None

        found=False

        for inode in self.cluster.nodes.keys():
            if (inode==nodename):
                self.cluster.nodes[inode].set_online(setstat)
                found=True
                self.response['response'] = 'OK'
                break

        if (not found):
            self.response['error'] = ("Host not found.")

        return None

    def _blocking_job(self):
        for job in self.queue:
            if job['priority'] == 'block' and job['status'] == 'wait':
                return job['pid']
        return None
    
    def _blocked_groups(self):
        bg=[]
        block_all=False
        for job in self.queue:
            reqs = job['require']
            if job['priority'] == 'block' and job['status'] == 'wait':
                
                req_groups = job._get_req_list(reqs, 'group')
                if (len(req_groups)==0):
                    ## Dude didn't specify group, we need to block all
                    block_all=True
                    break
                else:
                    for group in req_groups:
                        if group not in bg:
                            bg.append(group)
        if (block_all):
            for n in self.cluster.nodes.keys():
                #Enough to take 
                cg = self.cluster.nodes[n].get_groups()
                if (len(cg)>0):
                    ### Enough to add just the first group, this will block it
                    if cg[0] not in bg:
                        bg.append(cg[0])

        return bg



    def _process_submit_request(self, message):
        pid = message.get('pid')
        if pid is None:
            err="submit requests must contain the 'pid' field"
            self.response['error'] = err
            return

        req = message.get('require',None)
        if req  is None:
            err="submit requests must contain the 'require' field"
            self.response['error'] = err
            return

        # pass on the state
        keys = self.keys
        newjob = Job(message, **keys)

        # no side effects on cluster inside here
        newjob.match(self.cluster, self._blocked_groups())

        if newjob['status'] == 'nevermatch':
            self.response['error'] = newjob['reason']
        else:

            if not newjob.match_users(self.users):
                # the user limits would be exceeded (or something) if we run
                # this job
                newjob['status'] = 'wait'
                newjob['reason'] = 'user limits exceeded'
            elif newjob['status'] == 'ready':

                # only by reaching here to we reserve the hosts and
                # update user info
                self.cluster.reserve(newjob['hosts'])
                
                # keep statistics for each user
                self.users.increment_user(newjob['user'], newjob['hosts'])

            # this will create a pid.wait or pid.run depending on status if
            # status='ready', sets status to 'run' once the pid file is written
            newjob.spool()

            # if the status is 'run', the job will immediately
            # run. Otherwise it will wait and can't run till
            # we do a refresh
            self.queue.append(newjob)
            self.response['response'] = newjob['status']
            self.response['spool_fname']= \
                    newjob['spool_fname'].replace('wait','run')
            self.response['spool_wait']=newjob['spool_wait']
            if (self.response['response']=='run'):
                self.response['hosts']=newjob['hosts']
            elif self.response['response'] == 'wait':
                self.response['reason'] = newjob['reason']


    def _process_gethosts(self, message):
        pid = message.get('pid',None)
        if pid is None:
            self.response['error'] = \
                    "submit requests must contain the 'pid' field"
            return

        for job in self.queue:
            if job['pid']==pid:
                self.response['hosts']=job['hosts']
                self.response['response']='OK'
                return

        self.response['error'] = "we don't have this pid"
        return

        

    def _process_listing_request(self, message):
        """
        'user'
        'pid'
        'priority'
        'time_sub'
        'time_run'
        'status'
        'hosts'

        These we can maybe not send if we extract job_name
            or beginning of command
            'require'
            'commandline'
        """
        listing = []
        for job in self.queue:
            r = {}
            r['user'] = job['user']
            r['pid'] = job['pid']
            r['priority'] = job['priority']
            r['time_sub'] = job['time_sub']
            r['time_run'] = job['time_run']
            r['status'] = job['status']
            if r['status'] == 'run':
                r['hosts'] = job['hosts']
            else:
                # should we do this?
                r['hosts'] = []

            if 'reason' in job:
                r['reason'] = job['reason']
            if 'job_name' in job['require']:
                r['job_name'] = job['require']['job_name']
            else:
                r['job_name'] = job['commandline'].split()[0]
            listing.append(r)
        
        self.response['response'] = listing

    def _process_full_listing_request(self, message):
        """
        Send everything
        """
        listing = []
        for job in self.queue:
            listing.append(job.asdict())
        
        self.response['response'] = listing

    def _process_userlist_request(self):
        self.response['response'] = self.users.asdict()

    def _process_limit_request(self, message):
        """
        Currently only processing the limits entry
        """
        user = message.get('user',None)
        if user is None:
            self.response['error'] = ('You must send your username when '
                                      'setting user variables')
            return

        limits = message.get('limits',{}) 
        if not limits:
            #self.response['error'] = 'Expected some limits to be sent'
            return

        action=limits.pop('action','set')
        if action not in ['clear','set']:
            self.response['error'] = "action should be 'clear'or 'set'"
            return

        if self.verbosity > 1:
            print( 'limits sent:',limits )

        # we have a reference here, might want to hide this
        udata = self.users.get(user)

        if action=='clear':
            udata['limits'].clear()
        else:
            for l,v in limits.items():
                udata['limits'][l] = v

        self.save_users()
        self.response['response'] = 'OK'
        
    def _process_status_request(self, message):
        self.response['response'] = self.cluster.status()

    def _process_remove_request(self, message):
        self.refresh()

        pid = message.get('pid',None)
        user = message.get('user',None)
        if pid is None:
            self.response['error'] = \
                    "remove requests must contain the 'pid' field"
            return
        if user is None:
            self.response['error'] = \
                    "remove requests must contain the 'user' field"
            return
            
        if pid == 'all':
            self._process_remove_all_request(user)
        else:
            found = False
            for job in self.queue:
                if job['pid'] == pid:
                    # we don't actually remove anything, refresh will do it.
                    if (job['user']!=user and user!='root'):
                        self.response['error']=\
                                'PID belongs to user '+job['user']
                        return

                    self.response['response'] = 'OK'
                    self.response['pids_to_kill'] = [pid]
                    found=True
                    break
            if not found:
                self.response['error'] = 'pid %s not found' % pid

    def _process_remove_all_request(self, user):
        pids_to_kill=[]
        for job in self.queue:
            if job['user'] == user:
                pids_to_kill.append(job['pid'])
                # we rely on the refresh to do this
                #self._unreserve_job_and_decrement_user(job)
        self.response['response'] = 'OK'
        self.response['pids_to_kill'] = pids_to_kill

    def _process_notification(self, message):
        notifi = message.get('notification',None)
        if notifi is None:
            self.response['error'] = \
                    "notify requests must contain the 'notification' field"
            return

        if notifi == 'done':
            pid = message.get('pid',None)
            if pid is None:
                self.response['error'] = \
                        "remove requests must contain the 'pid' field"
                return
            self._remove_from_notify(pid)
            self.refresh()
        elif notifi == 'refresh':
            self.refresh()
        else:
            self.response['error'] = \
                    "Only support 'done' or 'refresh' notifications for now"
            return

    def _remove_from_notify(self, pid):
        """
        this is when the user has notified us the job is done.  we don't
        send a kill message back
        """
        found = False
        for i,job in enumerate(self.queue):
            if job['pid'] == pid:

                self._unreserve_job_and_decrement_user(job)

                del self.queue[i]
                self.response['response'] = 'OK'
                found=True
                break

        if not found:
            self.response['error'] = 'pid %s not found' % pid


    def _pid_exists(self, pid):        
        """ Check For the existence of a unix pid. """
        pid_path =  "/proc/%s" % pid
        if os.path.exists(pid_path):
            return True
        else:
            return False

def print_stat(status):
    """
    input status is the result of cluster.status
    """
    print
    nodes=status['nodes']
    lines=[]
    lens={}
    tot_active_cores=status['ncores']
    for k in ['usage','host','mem','groups']:
        lens[k] = len(k)
    for d in nodes:
        if d['online']==True:
            usage = '['+'*'*d['used']+'.'*(d['ncores']-d['used'])+']'
            l={'usage':usage,
               'host':d['hostname'],
               'mem':'%g' % d['mem'],
               'groups':','.join(d['grps'])}
            for n in lens:
                lens[n] = max(lens[n],len(l[n]))
            lines.append(l)
        elif d['online']==False:
            usage = '['+'X'*d['ncores']+']'
            l={'usage':usage,
               'host':d['hostname'],
               'mem':'%g' % d['mem'],
               'groups':','.join(d['grps'])}
            for n in lens:
                lens[n] = max(lens[n],len(l[n]))

            lines.append(l)
            tot_active_cores=tot_active_cores-d['ncores']

    fmt = ' %(usage)-'+str(lens['usage'])+'s  %(host)-'+str(lens['host'])+'s '
    fmt += ' %(mem)'+str(lens['mem'])+'s %(groups)-'+str(lens['groups'])+'s'
    hdr={}
    for k in lens:
        hdr[k]=k.capitalize()
    print( fmt % hdr )
    for l in lines:
        print( fmt % l )
    if (tot_active_cores>0):
        perc=100.*status['used']/tot_active_cores
    else: perc=00.00
    print
    mess=' Used/avail/active cores: %i/%i/%i (%3.1f%% load, %i are offline)'
    mess=mess % (status['used'],
                 tot_active_cores-status['used'],
                 tot_active_cores,
                 perc,
                 status['ncores']-tot_active_cores)

    print( mess )

def print_users(users):
    """
    input should be a dict.  You an convert a Users instance
    using asdict()
    """
    keys = ['user','Njobs','Ncores','limits']
    lens={}
    for k in keys:
        lens[k] = len(k)

    udata={}
    for uname in users:
        user=users[uname]
        udata[uname]={}
        udata[uname]['user'] = uname
        udata[uname]['Njobs'] = user['Njobs']
        udata[uname]['Ncores'] = user['Ncores']
        limits = user['limits']
        limits = '{' + ';'.join(['%s:%s' % (y,limits[y]) for y in limits]) +'}'
        udata[uname]['limits'] = limits

        for k in lens:
            lens[k] = max( lens[k],len(str(udata[uname][k])) )

    fmt =  ' %(user)-'+str(lens['user'])+'s'
    fmt += '  %(Njobs)-'+str(lens['Njobs'])+'s'
    fmt += '  %(Ncores)-'+str(lens['Ncores'])+'s'
    fmt += '  %(limits)-'+str(lens['limits'])+'s'

    hdr = {}
    for k in lens:
        hdr[k] = k.capitalize()
    print( fmt % hdr )
    for uname in sorted(udata):
        print( fmt % udata[uname] )

