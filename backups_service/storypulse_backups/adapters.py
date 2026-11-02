# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Fixed VeraCrypt/MEGAcmd adapters. Credentials never enter process arguments."""
from __future__ import annotations
import contextlib,fcntl,hashlib,json,os,pty,re,select,stat,struct,subprocess,termios,time
from pathlib import Path
from .core import BackupError,atomic_json,secure_read,strict_json,container_size,copy_snapshot,verify_snapshot_copy,validate_download_url

HOME=Path('/var/lib/storypulse-backups/mega')
FOLDER='/Backups-Historias'
HANDLE=re.compile(r'^H:[A-Za-z0-9_-]{8}$')
RUN=re.compile(r'^[a-f0-9]{32}$')

def digest(path):
 value=hashlib.sha256()
 with path.open('rb') as source:
  for chunk in iter(lambda:source.read(4*1024**2),b''):value.update(chunk)
 return value.hexdigest()

def vc(args,password=None,timeout=3600):
 command=['/usr/bin/veracrypt','-t','--non-interactive',*args]
 if password is not None:command+=['--stdin']
 try:p=subprocess.run(command,input=(password+'\n').encode() if password is not None else None,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=timeout)
 except (OSError,subprocess.TimeoutExpired):raise BackupError('VeraCrypt no pudo completar la operación.') from None
 if p.returncode:raise BackupError('VeraCrypt no pudo crear o abrir el contenedor. Revisá su configuración local.')

class CryptAdapter:
 def recover(self,work):
  for directory in work.iterdir():
   if not RUN.fullmatch(directory.name) or directory.is_symlink():continue
   mount=directory/'mounted'
   if os.path.ismount(mount):vc(['--unmount',str(mount)],timeout=120)

 def create_from_snapshot(self,snap,container,mount,password,progress):
  if any(c in password for c in ('\n','\r','\0')):raise BackupError('La contraseña de VeraCrypt tiene caracteres no admitidos.')
  allocated=container_size(snap)
  progress(1)
  vc(['--create',str(container),'--size='+str(allocated),'--encryption=AES','--hash=SHA-512','--filesystem=exFAT','--volume-type=normal','--pim=0','--keyfiles=','--random-source=/dev/urandom'],password)
  os.chmod(container,0o600);progress(35)
  common=['--pim=0','--keyfiles=','--protect-hidden=no','--fs-options=nodev,nosuid,noexec,umask=077']
  mounted=False
  try:
   vc([*common,str(container),str(mount)],password);mounted=True
   hashes=copy_snapshot(snap,mount,lambda value:progress(35+int(value*35/100)))
   vc(['--unmount',str(mount)],timeout=120);mounted=False
   vc([*common,'--mount-options=ro',str(container),str(mount)],password);mounted=True
   verify_snapshot_copy(snap,mount,hashes,lambda value:progress(70+int(value*30/100)))
   progress(100)
  finally:
   if mounted:vc(['--unmount',str(mount)],timeout=120)

class CloudAdapter:
 def __init__(self):
  self.env=dict(os.environ,HOME=str(HOME),LC_ALL='C',LANG='C')
  self.env.pop('MEGACMD_CONFIGDIR',None)
  self.current=None
  self.state_file=Path('/var/lib/storypulse-backups/state.json')

 def command(self,args,timeout=120,check=True):
  try:p=subprocess.run(['/usr/bin/mega-'+args[0],*args[1:]],capture_output=True,env=self.env,timeout=timeout)
  except (OSError,subprocess.TimeoutExpired):raise BackupError('MEGA no respondió. La copia anterior se conserva.') from None
  if p.returncode and check:
   self.command_diagnostic=dict(command=args[0],code=p.returncode,output=(p.stdout+p.stderr).decode(errors='replace')[-1000:])
   raise BackupError('MEGA rechazó la operación. Revisá la cuenta y el espacio disponible.')
  return p

 def _login(self,email,password):
  master,slave=pty.openpty();settings=termios.tcgetattr(slave);settings[3]&=~termios.ECHO;termios.tcsetattr(slave,termios.TCSANOW,settings)
  fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,120,0,0))
  def controlling_terminal():
   os.setsid();fcntl.ioctl(slave,termios.TIOCSCTTY,0)
  process=subprocess.Popen(['/usr/bin/mega-cmd'],stdin=slave,stdout=slave,stderr=slave,env=self.env,preexec_fn=controlling_terminal)
  os.close(slave);buf=b'';sent=False;asked=False;success=False;deadline=time.monotonic()+getattr(self,'login_timeout',120);last_probe=0
  try:
   while process.poll() is None and time.monotonic()<deadline:
    if select.select([master],[],[],.2)[0]:
     try:buf=(buf+os.read(master,8192))[-65536:]
     except OSError:break
     if not asked and b'MEGA CMD>' in buf:os.write(master,('login '+email+'\n').encode());asked=True;buf=b''
     if asked and not sent and b'password:' in buf.lower():
      time.sleep(.2);os.write(master,(password+'\r').encode());sent=True;buf=b''
     if b'authenticat' in buf.lower() and b'code' in buf.lower() and sent:raise BackupError('MEGA solicita autenticación adicional. Vinculá la cuenta desde el VPS.')
    if sent and time.monotonic()-last_probe>2:
     last_probe=time.monotonic();who=self.command(['whoami'],check=False)
     if who.returncode==0 and email.casefold().encode() in who.stdout.lower():success=True;os.write(master,b'quit --only-shell\n');break
   if process.poll() is None:process.terminate()
   process.wait(timeout=10)
   if not success:
    self.login_diagnostic=dict(command_sent=asked,password_sent=sent,exit_code=process.returncode,output=buf.decode(errors='replace').replace(password,'[redacted]').replace(email,'[redacted]')[-1000:])
    raise BackupError('No se pudo iniciar sesión en MEGA. Revisá las credenciales protegidas.')
  finally:
   if process.poll() is None:process.kill();process.wait()
   os.close(master)

 def ensure_session(self,email,password):
  if not isinstance(email,str) or not re.fullmatch(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+',email):raise BackupError('El correo de MEGA no es válido.')
  if any(c in password for c in ('\n','\r','\0')):raise BackupError('La contraseña de MEGA tiene caracteres no admitidos.')
  credential_hash=hashlib.sha256((email+'\0'+password).encode()).hexdigest()
  auth=HOME/'auth.json'
  previous=strict_json(secure_read(auth,8192,0,0o600)) if auth.exists() else {}
  owner=hashlib.sha256(email.casefold().encode()).hexdigest()
  if previous.get('owner') not in (None,owner):raise BackupError('La cuenta de MEGA cambió. Revisá la retención antes de vincular otra cuenta.')
  who=self.command(['whoami'],check=False)
  ready=who.returncode==0 and email.casefold().encode() in who.stdout.lower()
  if not ready or previous.get('credentials')!=credential_hash:
   if ready:self.command(['logout'])
   self._login(email,password)
   who=self.command(['whoami'])
   if email.casefold().encode() not in who.stdout.lower():raise BackupError('MEGA no confirmó la cuenta esperada.')
   atomic_json(auth,dict(owner=owner,credentials=credential_hash))
  created=self.command(['mkdir','-p',FOLDER],check=False)
  if created.returncode not in (0,54):raise BackupError('No se pudo preparar la carpeta de backups en MEGA.')
  self.command(['cd',FOLDER])
  self._recover_pending()

 def _pending(self):
  journal=HOME/'pending.json'
  if not journal.exists():return None
  value=strict_json(secure_read(journal,8192,0,0o600))
  run=value.get('run_id','');name=value.get('filename','')
  if set(value)!={'run_id','folder','filename'} or not isinstance(run,str) or not RUN.fullmatch(run) or value['folder']!=FOLDER+'/'+run or not isinstance(name,str) or not re.fullmatch(r'backup-\d{2}-\d{2}-\d{4}',name):raise BackupError('El registro de transferencia interrumpida no es válido.')
  return value

 def _recover_pending(self):
  value=self._pending()
  if value is None:return
  state=strict_json(secure_read(self.state_file,1024*1024,0,0o600)) if self.state_file.exists() else {}
  if any(item.get('run_id')==value['run_id'] and item.get('verified') is True for item in state.get('manifest',[]) if isinstance(item,dict)):
   (HOME/'pending.json').unlink();return
  removed=self.command(['rm','-r','-f',value['folder']],check=False)
  if removed.returncode and b'not found' not in (removed.stdout+removed.stderr).lower():raise BackupError('No se pudo limpiar la transferencia interrumpida. La copia anterior se conserva.')
  (HOME/'pending.json').unlink()

 def commit(self,remote):
  value=self._pending()
  if value is None:return
  if value['run_id']!=remote.get('run_id'):raise BackupError('El registro de transferencia no coincide con la copia verificada.')
  (HOME/'pending.json').unlink()

 def free_bytes(self):
  result=self.command(['df']).stdout.decode(errors='replace')
  current=re.search(r'^USED STORAGE:[ \t]*(\d+)[ \t]+[0-9.]+%[ \t]+of[ \t]+(\d+)[ \t]*$',result,re.M)
  if current:return max(0,int(current[2])-int(current[1]))
  match=re.search(r'^Total[ \t]*:[ \t]*([0-9]+)(?:[ \t]*B)?[ \t]*$',result,re.I|re.M)
  used=re.search(r'^Used[ \t]*:[ \t]*([0-9]+)(?:[ \t]*B)?[ \t]*$',result,re.I|re.M)
  if not match or not used:raise BackupError('No se pudo comprobar el espacio disponible en MEGA.')
  return max(0,int(match[1])-int(used[1]))

 def inspect(self,path):
  result=self.command(['ls','-l','--show-handles','--time-format=SHORT_UTC',path]).stdout.decode(errors='replace')
  rows=[]
  for line in result.splitlines():
   handle=re.search(r'\bH:[A-Za-z0-9_-]{8}\b',line)
   fields=re.match(r'^\s*([-dreisbxt][^\s]{3})\s+(\d+)\s+(\d+)\s+',line)
   if handle and fields and fields[1].startswith('-'):rows.append(dict(id=handle[0],size=int(fields[3])))
  if len(rows)!=1:raise BackupError('MEGA no confirmó un único archivo cifrado completo.')
  return rows[0]

 def upload(self,local,name,run_id,progress):
  if not RUN.fullmatch(run_id) or not re.fullmatch(r'backup-\d{2}-\d{2}-\d{4}',name):raise BackupError('El nombre de backup no es válido.')
  folder=FOLDER+'/'+run_id;path=folder+'/'+name
  if self._pending() is not None:raise BackupError('Hay una transferencia anterior pendiente de recuperación.')
  atomic_json(HOME/'pending.json',dict(run_id=run_id,folder=folder,filename=name))
  self.command(['mkdir',folder]);self.current=folder
  try:
   progress(0)
   process=subprocess.Popen(['/usr/bin/mega-put',str(local),folder],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=self.env)
   deadline=time.monotonic()+24*3600;buffer=b''
   try:
    while process.poll() is None:
     if time.monotonic()>deadline:raise BackupError('La subida a MEGA excedió el tiempo disponible.')
     if select.select([process.stdout],[],[],1)[0]:
      chunk=os.read(process.stdout.fileno(),8192);buffer=(buffer+chunk)[-16384:]
      values=re.findall(rb'(\d{1,3}(?:\.\d+)?)\s*%',buffer)
      if values:progress(min(99,int(float(values[-1]))))
    if process.returncode:raise BackupError('MEGA no completó la transferencia. La copia anterior se conserva.')
   finally:
    if process.poll() is None:process.terminate();process.wait(timeout=15)
    process.stdout.close()
   progress(100)
   row=self.inspect(path)
   if row['size']!=local.stat().st_size:raise BackupError('El tamaño subido a MEGA no coincide.')
   checksum=digest(local)
   self.command(['attr',row['id'],'-s','sp_sha',checksum])
   self.command(['attr',row['id'],'-s','sp_run',run_id])
   return dict(row,hash=checksum,folder=folder,run_id=run_id)
  except BaseException:
   with contextlib.suppress(BackupError):self.command(['rm','-r','-f',folder])
   raise

 def verify(self,remote,local):
  if not HANDLE.fullmatch(remote.get('id','')):return False
  check=self.inspect(remote['id'])
  saved=self.command(['attr',remote['id'],'sp_sha','--print-only-value']).stdout.decode().strip()
  return check['id']==remote['id'] and check['size']==local.stat().st_size and saved==digest(local)==remote['hash']

 def export_link(self,remote):
  """Publish only the exact owned, verified container; never a folder export."""
  failure='No se pudo preparar el enlace de descarga; se conserva la copia anterior.'
  if not isinstance(remote,dict) or not HANDLE.fullmatch(str(remote.get('id',''))) or not RUN.fullmatch(str(remote.get('run_id',''))):raise BackupError(failure)
  expected=remote.get('size',remote.get('bytes'))
  if type(expected) is not int or expected<=0:raise BackupError(failure)
  handle=remote['id']
  info=self.inspect(handle)
  if info['id']!=handle or info['size']!=expected:raise BackupError(failure)
  run=self.command(['attr',handle,'sp_run','--print-only-value']).stdout.decode().strip()
  if run!=remote['run_id']:raise BackupError(failure)
  def read_link(output):
   links=set()
   for token in output.decode(errors='replace').split():
    try:links.add(validate_download_url(token))
    except BackupError:pass
   if len(links)>1:raise BackupError(failure)
   return next(iter(links),None)
  result=self.command(['export',handle],check=False)
  existing=read_link(result.stdout) if result.returncode==0 else None
  if existing:return existing
  result=self.command(['export','-a','-f',handle],check=False)
  if result.returncode:
   result=self.command(['export',handle],check=False)
  link=read_link(result.stdout) if result.returncode==0 else None
  if link is None:raise BackupError(failure)
  return link

 def delete(self,remote_id):
  if not HANDLE.fullmatch(remote_id):raise BackupError('Identificador de copia remota no válido.')
  exists=self.command(['ls','-l',remote_id],check=False)
  if exists.returncode and b'not found' in (exists.stdout+exists.stderr).lower():return
  request=self.command(['attr',remote_id,'sp_run','--print-only-value'])
  run=request.stdout.decode().strip()
  if not RUN.fullmatch(run):raise BackupError('Se conservó un archivo remoto sin procedencia de backup.')
  self.command(['rm','-f',remote_id])
  folder=FOLDER+'/'+run
  listing=self.command(['ls',folder],check=False)
  if listing.returncode==0 and not listing.stdout.strip():self.command(['rm','-r','-f',folder])

def make_adapters(config):
 if not isinstance(config,dict) or set(config) not in ({'source'},{'source','bot_user'}) or config.get('source')!='/historys':raise BackupError('La configuración de backups no es válida.')
 if 'bot_user' in config and (not isinstance(config['bot_user'],str) or re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',config['bot_user']) is None):raise BackupError('El usuario del bot no es válido.')
 for executable in ('veracrypt','mega-login','mega-put','mega-ls','mkfs.exfat'):
  if not Path('/usr/bin/'+executable).is_file() and not Path('/usr/sbin/'+executable).is_file():raise BackupError('Faltan dependencias de backups en el servidor.')
 info=HOME.lstat()
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o700:raise BackupError('La sesión de MEGA no está en una carpeta privada.')
 return CryptAdapter(),CloudAdapter()
