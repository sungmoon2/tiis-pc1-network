#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sungmoon Park
# SPDX-License-Identifier: Apache-2.0
"""Owned synthetic Fabric bootstrap. Never targets an existing resource.

Development entrypoint: bootstrap.py RUNTIME IMAGE_LOCK OUTPUT NAMESPACE
The input runtime is a disposable copy, not an operating checkout or frozen repo.
All command receipts remain private until reviewed.
"""
import hashlib,json,os,re,secrets,subprocess,sys,time
from pathlib import Path
runtime=Path(sys.argv[1]).resolve()
images={i['role']:i['reference'] for i in json.loads(Path(sys.argv[2]).read_text())}
out=Path(sys.argv[3]).resolve();out.mkdir(exist_ok=False)
(out/'BOOTSTRAP_EXECUTED.py').write_bytes(Path(__file__).read_bytes())
namespace=sys.argv[4]
phase=sys.argv[5] if len(sys.argv)>5 else 'reproduce'
assert phase in ['start','test','collect','stop','reproduce']
assert re.fullmatch(r'tiispc1-[a-z0-9-]{6,32}',namespace)
assert re.fullmatch(r'runtime(?:-v[0-9]+)?',runtime.name) and not (runtime/'.git').exists()
assert (runtime/'api/package-lock.json').is_file()
if phase in ['start','reproduce']:assert not (runtime/'crypto').exists()
receipts=[];created=[];network_id=None;counter=0;owned_volumes=[]
baseline_volumes=set(subprocess.check_output(['docker','volume','ls','-q'],text=True).splitlines())
def save(name,value):
    temporary=out/(name+'.next')
    temporary.write_text(json.dumps(value,indent=2))
    temporary.replace(out/name)
def registry():
    state=dict(namespace=namespace,containers=created,network=network_id,volumes=owned_volumes)
    save('OWNED_RESOURCES.private.json',state)
    temp=runtime/'RESOURCE_REGISTRY.private.json.next'
    temp.write_text(json.dumps(state,indent=2));temp.chmod(0o600)
    temp.replace(runtime/'RESOURCE_REGISTRY.private.json')
def command(args,timeout=120,check=True):
    global counter
    counter+=1;start=time.time()
    try:
        p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
        result=dict(sequence=counter,argv=args,exit_code=p.returncode,stdout=p.stdout,
                    stderr=p.stderr,started_unix=start,ended_unix=time.time())
    except subprocess.TimeoutExpired as error:
        result=dict(sequence=counter,argv=args,exit_code=124,stdout=str(error.stdout or ''),
                    stderr=str(error.stderr or ''),started_unix=start,ended_unix=time.time())
    receipts.append(result);save('COMMANDS.private.json',receipts)
    if check and result['exit_code']: raise RuntimeError('command failed at sequence '+str(counter))
    return result
def docker(*args,**kw): return command(['docker',*map(str,args)],**kw)
def container(role,image,args,env=None,detached=False,aliases=(),memory='256m'):
    name=namespace+'-'+role
    assert not docker('ps','-aq','--filter','name=^/'+name+'$')['stdout'].strip(),'container collision'
    cmd=['run','--name',name,'--label','tiis.run='+namespace,
      '--network',namespace,'--memory',memory,'--memory-swap',memory,'--cpus','1',
      '--pids-limit','256','--security-opt','no-new-privileges']
    # Preserve each image's default working directory: some upstream entrypoints
    # chown their CWD. Mount only the data each service actually requires.
    if role.startswith('tool') or role.startswith('integration'):
        cmd+=['-v',str(runtime)+':/work','-w','/work']
    elif role=='chaincode':
        cmd+=['-v',str(runtime/'chaincode')+':/work/chaincode:ro']
    elif image in ['peer','orderer']:
        cmd+=['-v',str(runtime/'crypto')+':/work/crypto:ro']
        if image=='peer':
            cmd+=['-v',str(runtime/'peer-config')+':/work/peer-config:ro',
                  '-v',str(runtime/'network/builders')+':/work/network/builders:ro']
        else:cmd+=['-v',str(runtime/'genesis.block')+':/work/genesis.block:ro']
    elif image=='postgres':
        cmd+=['-v',str(runtime/'database-password')+':/run/secrets/database-password:ro']
    # Tool creates only new run-owned synthetic material. Runtime credentials never enter a candidate.
    if role.startswith(('tool','integration')): cmd+=['--user',str(os.getuid())+':'+str(os.getgid())]
    for alias in aliases:cmd+=['--network-alias',alias]
    for k,v in (env or {}).items():cmd+=['-e',k+'='+str(v)]
    if detached:cmd+=['-d']
    cmd += [images[image],*args]
    result=docker(*cmd,check=False,timeout=300 if role.startswith('integration') else 180)
    found=docker('ps','-aq','--filter','name=^/'+name+'$')['stdout'].strip()
    if found:
        obj=json.loads(docker('inspect',found)['stdout'])[0]
        assert obj['Config']['Labels'].get('tiis.run')==namespace
        for mount in obj['Mounts']:
            if mount['Type']=='volume':
                assert mount['Name'] not in baseline_volumes,'existing volume attachment prohibited'
                owned_volumes.append(mount['Name'])
        created.append(obj['Id']);registry()
    if result['exit_code']: raise RuntimeError('container failed: '+role)
    return result['stdout'].strip()
def tool(args,env=None):
    role='tool-'+str(counter+1)
    result=container(role,'tools',args,env=env)
    obj=json.loads(docker('inspect',namespace+'-'+role)['stdout'])[0]
    assert obj['Config']['Labels'].get('tiis.run')==namespace
    docker('rm','-v',obj['Id'])
    return result
def peer_env(short):
    msp={'a':'SourceAMSP','b':'SourceBMSP','t':'TargetTMSP'}[short]
    domain=short+'.artifact.test'
    return dict(CORE_PEER_LOCALMSPID=msp,CORE_PEER_ADDRESS='peer0.'+domain+':7051',
      CORE_PEER_TLS_ENABLED='true',
      CORE_PEER_TLS_ROOTCERT_FILE='/work/crypto/peerOrganizations/'+domain+'/peers/peer0.'+domain+'/tls/ca.crt',
      CORE_PEER_MSPCONFIGPATH='/work/crypto/peerOrganizations/'+domain+'/users/Admin@'+domain+'/msp')
def wait_container(cid):
    obj=json.loads(docker('inspect',cid)['stdout'])[0]
    if not obj['State']['Running']:
        raise RuntimeError('service exited: '+obj['Name'])
def teardown():
    failed=[]
    for cid in reversed(created):
        result=docker('inspect',cid,check=False)
        if result['exit_code']:continue
        obj=json.loads(result['stdout'])[0]
        if obj['Config']['Labels'].get('tiis.run')!=namespace:
            failed.append(dict(id=cid,error='label mismatch'));continue
        logs=docker('logs','--tail','2000',cid,check=False)
        save('LOG-'+obj['Name'].strip('/')+'.private.json',logs)
        if docker('rm','-f','-v',cid,check=False)['exit_code']:failed.append(dict(id=cid,error='remove failed'))
    if network_id:
        result=docker('network','inspect',network_id,check=False)
        if not result['exit_code']:
            obj=json.loads(result['stdout'])[0]
            if obj['Labels'].get('tiis.run')==namespace:
                if docker('network','rm',network_id,check=False)['exit_code']:
                    failed.append(dict(id=network_id,error='network remove failed'))
            else: failed.append(dict(id=network_id,error='network label mismatch'))
    remaining_containers=docker('ps','-aq','--filter','label=tiis.run='+namespace)['stdout'].splitlines()
    remaining_volumes=sorted(set(owned_volumes)&set(docker('volume','ls','-q')['stdout'].splitlines()))
    remaining_networks=docker('network','ls','-q','--filter','label=tiis.run='+namespace)['stdout'].splitlines()
    if remaining_containers or remaining_volumes or remaining_networks:
        failed.append(dict(error='owned resources remain'))
    save('TEARDOWN.json',dict(status='PASS' if not failed else 'FAIL',failures=failed,
      owned_container_ids=created,owned_network_id=network_id,volumes_created=owned_volumes,
      remaining_containers=remaining_containers,remaining_volumes=remaining_volumes,remaining_networks=remaining_networks,
      note='Only recorded label-verified synthetic resources removed; runtime evidence retained'))
    return not failed
def run_tests():
    result=container('integration','node',['node','/work/artifact/tests/integration.cjs'],
       dict(ARTIFACT_MODE='synthetic-local'),memory='768m')
    save('INTEGRATION_STDOUT.json',dict(stdout=result))
    result=container('integration-http','node',['node','/work/artifact/tests/http-regression.cjs'],
       dict(ARTIFACT_MODE='synthetic-local'),memory='768m')
    save('HTTP_STDOUT.json',dict(stdout=result))
if phase in ['test','collect','stop']:
    state=json.loads((runtime/'RESOURCE_REGISTRY.private.json').read_text())
    assert state['namespace']==namespace
    created=state['containers'];network_id=state['network'];owned_volumes=state['volumes']
    if phase=='stop':
        ok=teardown();save('RESULT.json',dict(status='PASS' if ok else 'FAIL',phase=phase))
        sys.exit(0 if ok else 6)
    if phase=='collect':
        for cid in created:
            r=docker('inspect',cid,check=False)
            if r['exit_code']:continue
            obj=json.loads(r['stdout'])[0]
            assert obj['Config']['Labels'].get('tiis.run')==namespace
            save('LOG-'+obj['Name'].strip('/')+'.private.json',docker('logs',cid,check=False))
        save('RESULT.json',dict(status='PASS',phase=phase));sys.exit(0)
    try:
        run_tests();save('RESULT.json',dict(status='PASS',phase=phase))
    except Exception as exc:
        save('RESULT.json',dict(status='FAIL',phase=phase,error=str(exc)));sys.exit(4)
    sys.exit(0)
status='FAIL';error=None
try:
    assert not docker('network','ls','-q','--filter','name=^'+namespace+'$')['stdout'].strip()
    available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                       if line.startswith('MemAvailable:')))*1024
    virtualization=subprocess.run(['systemd-detect-virt','--vm'],capture_output=True,text=True)
    guest=virtualization.returncode==0 and virtualization.stdout.strip() in ['kvm','qemu']
    threshold=(4 if guest else 9)*1024**3
    assert available>threshold,'resource reserve changed'
    save('RESOURCE_PROFILE.json',dict(guest=guest,available_bytes=available,minimum_available=threshold,
      max_test_memory_bytes=4*1024**3,host_reserve_min_bytes=5*1024**3 if not guest else None))
    network_id=docker('network','create','--internal','--label','tiis.run='+namespace,namespace)['stdout'].strip()
    registry()
    crypto=dict(OrdererOrgs=[dict(Name='Orderer',Domain='orderer.artifact.test',
      EnableNodeOUs=True,Specs=[dict(Hostname='orderer')])],
      PeerOrgs=[dict(Name=msp,Domain=short+'.artifact.test',EnableNodeOUs=True,
      Template=dict(Count=1),Users=dict(Count=1)) for short,msp in
      [('a','SourceA'),('b','SourceB'),('t','TargetT')]])
    (runtime/'crypto-config.yaml').write_text(json.dumps(crypto,indent=2))
    tool(['cryptogen','generate','--config=/work/crypto-config.yaml','--output=/work/crypto'])
    # Reuse only the pinned upstream runtime's default configuration in this disposable
    # runtime; never include that third-party configuration in the clean source repository.
    core=tool(['cat','/etc/hyperledger/fabric/core.yaml'])
    replacement='externalBuilders:\n        - name: artifact-ccaas\n          path: /work/network/builders/ccaas'
    core,n=re.subn(r'externalBuilders:\n       - name: ccaas_builder\n         path: /opt/hyperledger/ccaas_builder\n         propagateEnvironment:\n           - CHAINCODE_AS_A_SERVICE_BUILDER_CONFIG',
                  replacement,core)
    assert n==1,'unsupported peer configuration shape'
    (runtime/'peer-config').mkdir()
    (runtime/'peer-config/core.yaml').write_text(core)
    def policies(msp):
        return {key:dict(Type='Signature',Rule="OR('"+msp+"."+role+"')") for key,role in
                [('Readers','member'),('Writers','member'),('Admins','admin'),('Endorsement','peer')]}
    orgs=[]
    for short,msp in [('a','SourceAMSP'),('b','SourceBMSP'),('t','TargetTMSP')]:
        orgs.append(dict(Name=msp,ID=msp,MSPDir='/work/crypto/peerOrganizations/'+short+'.artifact.test/msp',
          Policies=policies(msp),AnchorPeers=[dict(Host='peer-'+short,Port=7051)]))
    orderer_org=dict(Name='OrdererMSP',ID='OrdererMSP',
      MSPDir='/work/crypto/ordererOrganizations/orderer.artifact.test/msp',Policies=policies('OrdererMSP'),
      OrdererEndpoints=['orderer.orderer.artifact.test:7050'])
    implicit={k:dict(Type='ImplicitMeta',Rule=v) for k,v in
              [('Readers','ANY Readers'),('Writers','ANY Writers'),('Admins','MAJORITY Admins')]}
    tls='/work/crypto/ordererOrganizations/orderer.artifact.test/orderers/orderer.orderer.artifact.test/tls/'
    orderer=dict(OrdererType='etcdraft',Addresses=['orderer.orderer.artifact.test:7050'],BatchTimeout='500ms',
      BatchSize=dict(MaxMessageCount=10,AbsoluteMaxBytes='10 MB',PreferredMaxBytes='512 KB'),
      EtcdRaft=dict(Consenters=[dict(Host='orderer.orderer.artifact.test',Port=7050,ClientTLSCert=tls+'server.crt',
                                   ServerTLSCert=tls+'server.crt')]),
      Organizations=[orderer_org],Policies={**implicit,'BlockValidation':dict(Type='ImplicitMeta',Rule='ANY Writers')},
      Capabilities={'V2_0':True})
    app=dict(Organizations=orgs,Policies={**implicit,
      'LifecycleEndorsement':dict(Type='ImplicitMeta',Rule='MAJORITY Endorsement'),
      'Endorsement':dict(Type='ImplicitMeta',Rule='ANY Endorsement')},Capabilities={'V2_5':True})
    config=dict(Profiles={
      'ArtifactGenesis':dict(Policies=implicit,Capabilities={'V2_0':True},Orderer=orderer,
        Consortiums={'ArtifactConsortium':dict(Organizations=orgs)}),
      'ArtifactChannel':dict(Consortium='ArtifactConsortium',Policies=implicit,
        Capabilities={'V2_0':True},Application=app)})
    (runtime/'configtx.yaml').write_text(json.dumps(config,indent=2))
    tool(['configtxgen','-configPath','/work','-profile','ArtifactGenesis','-channelID','artifact-system',
          '-outputBlock','/work/genesis.block'])
    tool(['configtxgen','-configPath','/work','-profile','ArtifactChannel','-channelID','artifact',
          '-outputCreateChannelTx','/work/channel.tx'])
    oe=dict(ORDERER_GENERAL_LISTENADDRESS='0.0.0.0',ORDERER_GENERAL_LISTENPORT='7050',
      ORDERER_GENERAL_LOCALMSPID='OrdererMSP',
      ORDERER_GENERAL_LOCALMSPDIR='/work/crypto/ordererOrganizations/orderer.artifact.test/orderers/orderer.orderer.artifact.test/msp',
      ORDERER_GENERAL_BOOTSTRAPMETHOD='file',ORDERER_GENERAL_BOOTSTRAPFILE='/work/genesis.block',
      ORDERER_GENERAL_TLS_ENABLED='true',ORDERER_GENERAL_TLS_CERTIFICATE=tls+'server.crt',
      ORDERER_GENERAL_TLS_PRIVATEKEY=tls+'server.key',ORDERER_GENERAL_TLS_ROOTCAS='['+tls+'ca.crt]',
      ORDERER_GENERAL_CLUSTER_CLIENTCERTIFICATE=tls+'server.crt',
      ORDERER_GENERAL_CLUSTER_CLIENTPRIVATEKEY=tls+'server.key',
      ORDERER_GENERAL_CLUSTER_ROOTCAS='['+tls+'ca.crt]',
      ORDERER_CHANNELPARTICIPATION_ENABLED='false')
    orderer_id=container('orderer','orderer',['orderer'],oe,True,['orderer','orderer.orderer.artifact.test'],memory='256m')
    password=secrets.token_hex(24)
    (runtime/'database-password').write_text(password)
    (runtime/'database-password').chmod(0o600)
    container('postgres','postgres',['postgres'],dict(POSTGRES_USER='artifact',POSTGRES_DB='artifact',
      POSTGRES_PASSWORD_FILE='/run/secrets/database-password'),True,['postgres'],memory='192m')
    container('redis','redis',['redis-server','--save','','--appendonly','no'],detached=True,
      aliases=['redis'],memory='96m')
    peer_ids=[]
    for short in ['a','b','t']:
        container('couch-'+short,'couchdb',['/opt/couchdb/bin/couchdb'],
          dict(COUCHDB_USER='artifact',COUCHDB_PASSWORD=password),True,['couch-'+short],memory='192m')
        domain=short+'.artifact.test'
        env=peer_env(short)
        env.update(FABRIC_CFG_PATH='/work/peer-config',CORE_PEER_ID='peer-'+short,
          CORE_PEER_LISTENADDRESS='0.0.0.0:7051',CORE_PEER_CHAINCODEADDRESS='peer-'+short+':7052',
          CORE_PEER_CHAINCODELISTENADDRESS='0.0.0.0:7052',
          CORE_PEER_MSPCONFIGPATH='/work/crypto/peerOrganizations/'+domain+'/peers/peer0.'+domain+'/msp',
          CORE_PEER_TLS_CERT_FILE='/work/crypto/peerOrganizations/'+domain+'/peers/peer0.'+domain+'/tls/server.crt',
          CORE_PEER_TLS_KEY_FILE='/work/crypto/peerOrganizations/'+domain+'/peers/peer0.'+domain+'/tls/server.key',
          CORE_PEER_GOSSIP_EXTERNALENDPOINT='peer0.'+domain+':7051',
          CORE_PEER_GOSSIP_BOOTSTRAP='peer0.'+domain+':7051',
          CORE_PEER_GOSSIP_USELEADERELECTION='false',CORE_PEER_GOSSIP_ORGLEADER='true',
          CORE_OPERATIONS_LISTENADDRESS='0.0.0.0:9443',
          CORE_LEDGER_STATE_STATEDATABASE='CouchDB',
          CORE_LEDGER_STATE_COUCHDBCONFIG_COUCHDBADDRESS='couch-'+short+':5984',
          CORE_LEDGER_STATE_COUCHDBCONFIG_USERNAME='artifact',
          CORE_LEDGER_STATE_COUCHDBCONFIG_PASSWORD=password)
        peer_ids.append(container('peer-'+short,'peer',['peer','node','start'],env,True,
          ['peer-'+short,'peer0.'+domain],memory='384m'))
    for cid in peer_ids+[orderer_id]: wait_container(cid)
    orderer_tls=['--tls','--cafile',tls+'ca.crt']
    tool(['peer','channel','create','-o','orderer.orderer.artifact.test:7050',*orderer_tls,'-c','artifact','-f','/work/channel.tx',
          '--outputBlock','/work/artifact.block','--timeout','30s'],peer_env('a'))
    for short in ['a','b','t']:
        tool(['peer','channel','join','-b','/work/artifact.block'],peer_env(short))
    package_id='evidence_0.1.0:'+hashlib.sha256((runtime/'evidence.tgz').read_bytes()).hexdigest()
    for short in ['a','b','t']:
        tool(['peer','lifecycle','chaincode','install','/work/evidence.tgz'],peer_env(short))
    container('chaincode','node',['node','/work/chaincode/src/server.js'],
      dict(ARTIFACT_MODE='synthetic-local',CHAINCODE_ID=package_id),True,['chaincode'],memory='384m')
    policy="OR('SourceAMSP.peer','SourceBMSP.peer','TargetTMSP.peer')"
    common=['-o','orderer.orderer.artifact.test:7050',*orderer_tls,'--channelID','artifact','--name','evidence',
            '--version','0.1.0','--sequence','1','--signature-policy',policy]
    for short in ['a','b','t']:
        tool(['peer','lifecycle','chaincode','approveformyorg',*common,
              '--package-id',package_id],peer_env(short))
    endpoints=[]
    for short in ['a','b','t']:
        endpoints+=['--peerAddresses','peer0.'+short+'.artifact.test:7051','--tlsRootCertFiles',
           '/work/crypto/peerOrganizations/'+short+'.artifact.test/peers/peer0.'+short+'.artifact.test/tls/ca.crt']
    tool(['peer','lifecycle','chaincode','commit',*common,*endpoints],peer_env('a'))
    identities={}
    for name,short,enrollment,attribute,ou in [
        ('writer-a','a','writer-a','true','client'),('writer-b','b','writer-b','true','client'),
        ('generic-a','a','generic-a',None,'client'),('wrong-a','a','not-writer','true','client'),
        ('admin-a','a','writer-a','true','admin'),('reader','t','reader',None,'client')]:
        directory=runtime/'identities'/name;directory.mkdir(parents=True,mode=0o700)
        ca=runtime/'crypto/peerOrganizations'/(short+'.artifact.test')/'ca'
        ca_key=list(ca.glob('*_sk'));assert len(ca_key)==1
        ca_cert=list(ca.glob('*.pem'));assert len(ca_cert)==1
        attrs={'hf.EnrollmentID':enrollment}
        if attribute:attrs['artifact.integrationWriter']=attribute
        raw=json.dumps({'attrs':attrs},separators=(',',':')).encode()
        ext='basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n1.2.3.4.5.6.7.8.1=DER:'+':'.join(f'{b:02x}' for b in raw)+'\n'
        (directory/'extensions.cnf').write_text(ext)
        command(['openssl','ecparam','-name','prime256v1','-genkey','-noout','-out',str(directory/'key.pem')])
        (directory/'key.pem').chmod(0o600)
        command(['openssl','req','-new','-key',str(directory/'key.pem'),'-out',str(directory/'request.csr'),
                 '-subj','/C=ZZ/O=Synthetic/OU='+ou+'/CN='+name])
        command(['openssl','x509','-req','-in',str(directory/'request.csr'),'-CA',str(ca_cert[0]),
          '-CAkey',str(ca_key[0]),'-set_serial','0x'+secrets.token_hex(16),'-days','2',
          '-extfile',str(directory/'extensions.cnf'),'-out',str(directory/'cert.pem')])
        identities[name]=dict(mspId={'a':'SourceAMSP','b':'SourceBMSP','t':'TargetTMSP'}[short],
          endpoint='peer-'+short+':7051',certificate='/work/identities/'+name+'/cert.pem',
          key='/work/identities/'+name+'/key.pem',
          tlsRootCertificate='/work/crypto/peerOrganizations/'+short+'.artifact.test/peers/peer0.'+short+'.artifact.test/tls/ca.crt')
    (runtime/'identities.json').write_text(json.dumps(identities,indent=2))
    if phase=='reproduce':run_tests()
    status='PASS'
except Exception as exc:
    error=str(exc);save('FAILURE.json',dict(error=error));print(error,flush=True)
finally:
    cleanup=teardown() if phase=='reproduce' or status!='PASS' else False
    save('RESULT.json',dict(status=status,error=error,teardown_complete=cleanup,
      phase=phase,full_claims_executed=False,stage='NETWORK_PHASE_NOT_G3_ATTESTATION'))
sys.exit(0 if status=='PASS' and (cleanup or phase=='start') else 3)
