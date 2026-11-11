"""ContaSaas: servidor, persistência e lembretes. Python 3.12+, sem dependências."""
import argparse
import core
import threading
import re
from urllib.parse import quote
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('CONTASAAS_DATA', ROOT / 'data'))
MAX_BODY = 12 * 1024 * 1024
SCHEMA = '''
CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, tax_id TEXT NOT NULL DEFAULT '', regime TEXT NOT NULL DEFAULT 'Simples Nacional');
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id), title TEXT NOT NULL, due TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pendente', filename TEXT, storage TEXT, mime TEXT);
CREATE TABLE IF NOT EXISTS obligations(id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id), title TEXT NOT NULL, due TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pendente');
CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id), kind TEXT NOT NULL CHECK(kind IN ('bank','book')), date TEXT NOT NULL, description TEXT NOT NULL, cents INTEGER NOT NULL, fingerprint TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS matches(id INTEGER PRIMARY KEY, bank_id INTEGER UNIQUE NOT NULL REFERENCES entries(id), book_id INTEGER UNIQUE NOT NULL REFERENCES entries(id));
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(address TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id), day TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Na fila', error TEXT, UNIQUE(client_id,day));
'''

def db():
    return core.connect(DATA)


def init():
    with db() as con:
        con.executescript(SCHEMA)
        core.migrate(con, DATA)


password_hash = core.password_hash


def setup(password, username='admin', office_name='Meu escritório', office_id=1):
    if office_id<1 or not office_name.strip() or len(office_name)>200:
        raise ValueError('Informe um escritório válido e id positivo.')
    salt, digest = core.password_fields(password)
    username = username.strip().lower()
    if not __import__('re').fullmatch(r'[a-z0-9@._+\-]{3,100}', username):
        raise ValueError('Login inválido.')
    with db() as con:
        con.execute('INSERT OR IGNORE INTO offices(id,name) VALUES(?,?)',(office_id,office_name))
        con.execute('INSERT OR IGNORE INTO reminder_settings(office_id) VALUES(?)',(office_id,))
        existing=con.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
        if existing and (existing['office_id']!=office_id or existing['role']!='admin'):
            raise ValueError('Login já pertence a outro acesso.')
        if existing:
            con.execute('UPDATE users SET salt=?,password_hash=?,active=1,must_change=0 WHERE id=?',(salt,digest,existing['id']))
            con.execute('DELETE FROM sessions WHERE user_id=?',(existing['id'],))
        else:
            con.execute("INSERT INTO users(office_id,username,name,role,salt,password_hash,must_change) VALUES(?,?,?,'admin',?,?,0)",(office_id,username,'Administrador',salt,digest))
        con.execute('UPDATE offices SET name=? WHERE id=?',(office_name,office_id))
    # O arquivo legado deixa de ser uma fonte de autenticação após a migração.
    (DATA/'auth.json').unlink(missing_ok=True)



def seed():
    with db() as con:
        if con.execute('SELECT COUNT(*) FROM clients').fetchone()[0]:
            raise ValueError('A demonstração só pode ser criada em um banco vazio.')
        for name, email, tax, regime in [('Aurora Design', 'financeiro@aurora.example', '00.000.000/0001-00', 'Simples Nacional'), ('Café do Centro', 'contato@cafe.example', '11.111.111/0001-11', 'Simples Nacional'), ('Horizonte Tech', 'fiscal@horizonte.example', '22.222.222/0001-22', 'Lucro Presumido')]:
            con.execute('INSERT INTO clients(name,email,tax_id,regime) VALUES(?,?,?,?)', (name,email,tax,regime))
        today = date.today()
        for client, title, offset in [(1,'Extrato bancário do mês',-2),(1,'Notas fiscais de serviços',3),(2,'Comprovantes de despesas',1),(3,'Notas fiscais de entrada',5)]:
            con.execute('INSERT INTO documents(client_id,title,due) VALUES(?,?,?)', (client,title,str(today+timedelta(days=offset))))
        for client,title,offset,owner in [(1,'DAS · Simples Nacional',3,'Marina'),(2,'Folha de pagamento',1,'Rafael'),(3,'EFD-Contribuições',7,'Marina'),(1,'DCTFWeb',-1,'Rafael')]:
            con.execute('INSERT INTO obligations(client_id,title,due,owner) VALUES(?,?,?,?)', (client,title,str(today+timedelta(days=offset)),owner))
        for kind,desc,cents in [('bank','Recebimento projeto Aurora',480000),('book','Serviço de design',480000),('bank','Licença de software',-18990),('book','Assinatura mensal',-18990),('bank','Transferência a identificar',-75000)]:
            con.execute('INSERT INTO entries(client_id,kind,date,description,cents,fingerprint) VALUES(1,?,?,?,?,?)', (kind,str(today),desc,cents,secrets.token_hex(16)))


def valid_date(value):
    return date.fromisoformat(str(value)).isoformat()


def parse_csv(text):
    """Valida o lote inteiro antes de gravar. Valores em reais, precisão de centavos."""
    stream = io.StringIO(text.lstrip('\ufeff'))
    sample = text[:4096]
    if not sample.strip():
        raise ValueError('O CSV está vazio.')
    delimiter = ';' if sample.splitlines()[0].count(';') > sample.splitlines()[0].count(',') else ','
    reader = csv.DictReader(stream, delimiter=delimiter)
    if reader.fieldnames != ['data','descricao','valor']:
        raise ValueError('Cabeçalho esperado: data;descricao;valor')
    result = []
    for line,row in enumerate(reader,2):
        if line > 5001:
            raise ValueError('Limite de 5.000 linhas por importação.')
        try:
            dt = valid_date(row['data'])
            desc = row['descricao'].strip()
            raw = row['valor'].strip()
            if ',' in raw:
                raw = raw.replace('.','').replace(',','.')
            value = Decimal(raw)
            if not value.is_finite() or value != value.quantize(Decimal('.01')) or abs(value)>Decimal('999999999.99') or not desc or len(desc)>300:
                raise ValueError()
            result.append((dt,desc,int(value*100)))
        except (ValueError, TypeError, AttributeError, InvalidOperation):
            raise ValueError(f'Linha {line} inválida. Use data AAAA-MM-DD e valor com até duas casas decimais.')
    if not result:
        raise ValueError('O CSV está vazio.')
    return result


def queue_reminders(con):
    return core.queue_reminders(con)


def send_reminders():
    return core.send_reminders(DATA)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        print(f'{self.address_string()} {self.command} {urlparse(self.path).path}')

    def respond(self, status, data, mime='application/json; charset=utf-8', headers=None):
        body=json.dumps(data,ensure_ascii=False).encode() if mime.startswith('application/json') else data
        self.send_response(status)
        for k,v in {'Content-Type':mime,'Content-Length':str(len(body)),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",**(headers or {})}.items():
            self.send_header(k,v)
        self.end_headers()
        self.wfile.write(body)

    def payload(self):
        n=int(self.headers.get('Content-Length','0'))
        if not 0<n<=MAX_BODY:
            raise ValueError('Requisição vazia ou maior que 12 MB.')
        if not self.headers.get('Content-Type','').startswith('application/json'):
            raise ValueError('Use JSON na requisição.')
        value=json.loads(self.rfile.read(n))
        if not isinstance(value,dict):
            raise ValueError('Objeto JSON esperado.')
        return value

    def session(self, con):
        try:
            token=SimpleCookie(self.headers.get('Cookie',''))['session'].value
        except (KeyError,ValueError):
            return None
        return con.execute('SELECT s.token,s.csrf,s.expires,u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.active=1',
                           (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()

    def do_GET(self):
        try:
            path=urlparse(self.path).path
            static={'/':'index.html','/portal':'index.html','/app.js':'app.js','/v2.js':'v2.js','/style.css':'style.css'}
            if path in static:
                p=ROOT/'static'/static[path]
                mime={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'}[p.suffix]
                return self.respond(200,p.read_bytes(),mime)
            with db() as con:
                if path=='/health':
                    con.execute('SELECT 1').fetchone()
                    return self.respond(200,{'status':'ok'})
                sess=self.session(con)
                if not sess:
                    return self.respond(401,{'error':'Entre para acessar o ContaSaas.'})
                if path=='/api/state':
                    return self.respond(200,core.state_for(con,sess))
                if sess['must_change']:
                    return self.respond(403,{'error':'Troque sua senha temporária antes de continuar.'})
                if path=='/api/operations':
                    if sess['role']!='admin':
                        raise PermissionError('Apenas administradores podem acessar a operação.')
                    worker=con.execute('SELECT * FROM worker_status WHERE id=1').fetchone()
                    # Backups físicos incluem todos os escritórios: somente o operador do servidor acessa os arquivos.
                    folder=DATA/'backups'
                    backups=sorted(folder.glob('*.zip')) if folder.exists() else []
                    return self.respond(200,{'worker':dict(worker) if worker else None,'backup_count':len(backups),'last_backup':backups[-1].name if backups else None})
                if path.startswith('/api/download/'):
                    doc=core.scoped(con,'documents',path.rsplit('/',1)[1],sess)
                    if not doc['storage']:
                        return self.respond(404,{'error':'Arquivo não encontrado.'})
                    return self.download(doc)
                if path.startswith('/api/version/'):
                    version=core.scoped(con,'file_versions',path.rsplit('/',1)[1],{**dict(sess),'role':'staff'})
                    core.scoped(con,'documents',version['document_id'],sess)
                    return self.download(version)
            self.respond(404,{'error':'Recurso não encontrado.'})
        except PermissionError as e:
            self.respond(403,{'error':str(e)})
        except (ValueError,OSError):
            self.respond(400,{'error':'Não foi possível carregar o recurso.'})
        except Exception:
            self.respond(500,{'error':'Erro ao carregar dados.'})
            import traceback
            traceback.print_exc()

    def download(self, row):
        return self.respond(200,(DATA/'uploads'/row['storage']).read_bytes(),'application/octet-stream',
                            {'Content-Disposition':"attachment; filename*=UTF-8''"+quote(row['filename'])})

    def login(self,con,v):
        origin=self.headers.get('Origin')
        if origin and urlparse(origin).netloc!=self.headers.get('Host'):
            return self.respond(403,{'error':'Origem inválida.'})
        address=self.client_address[0]
        attempt=con.execute('SELECT * FROM login_attempts WHERE address=? AND reset>?',(address,time.time())).fetchone()
        if attempt and attempt['attempts']>=10:
            return self.respond(429,{'error':'Muitas tentativas. Aguarde 15 minutos.'})
        username=str(v.get('username','admin')).strip().lower()
        password=str(v.get('password',''))
        if len(password)>256:
            raise ValueError('Senha maior que o limite permitido.')
        user=con.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
        salt=user['salt'] if user else '0'*32
        digest=password_hash(password,salt)
        if not user or not user['active'] or not hmac.compare_digest(digest,user['password_hash']):
            con.execute('INSERT INTO login_attempts VALUES(?,1,?) ON CONFLICT(address) DO UPDATE SET attempts=CASE WHEN reset<? THEN 1 ELSE attempts+1 END,reset=CASE WHEN reset<? THEN ? ELSE reset END',(address,time.time()+900,time.time(),time.time(),time.time()+900))
            con.commit()
            return self.respond(401,{'error':'Login ou senha inválidos.'})
        con.execute('DELETE FROM login_attempts WHERE address=?',(address,))
        token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        con.execute('DELETE FROM sessions WHERE expires<=?',(time.time(),))
        con.execute('INSERT INTO sessions(token,csrf,expires,user_id) VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,time.time()+28800,user['id']))
        core.audit(con,user,'login','sessão')
        con.commit()
        secure='; Secure' if os.environ.get('COOKIE_SECURE')=='1' else ''
        return self.respond(200,{'ok':True},headers={'Set-Cookie':f'session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800{secure}'})

    def do_POST(self):
        try:
            path=urlparse(self.path).path
            v=self.payload()
            with db() as con:
                if path=='/api/login':
                    return self.login(con,v)
                # Serializa validação de permissões e alteração contra desativação concorrente.
                con.execute('BEGIN IMMEDIATE')
                sess=self.session(con)
                if not sess:
                    return self.respond(401,{'error':'Sua sessão expirou. Entre novamente.'})
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),sess['csrf']):
                    return self.respond(403,{'error':'Atualize a página e tente novamente.'})
                if sess['must_change'] and path not in ('/api/password','/api/logout'):
                    raise PermissionError('Troque sua senha temporária antes de continuar.')
                if sess['role']=='client' and path not in ('/api/logout','/api/password','/api/upload'):
                    raise PermissionError('Seu perfil de cliente permite consultar pendências e enviar arquivos.')
                if path in ('/api/users','/api/user-update','/api/settings') and sess['role']!='admin':
                    raise PermissionError('Apenas administradores podem realizar esta ação.')
                result=self.mutate(con,path,v,sess)
                # Nenhum corpo de requisição é registrado: pode conter senha ou documento.
                core.audit(con,sess,path.removeprefix('/api/'),v.get('id',v.get('client_id','escritório')),json.dumps({k:v[k] for k in ('status','role','due','enabled','active','client_id','days_before','interval_days') if k in v},ensure_ascii=False))
                con.commit()
                return self.respond(200,result)
        except PermissionError as e:
            self.respond(403,{'error':str(e)})
        except (ValueError,TypeError,KeyError,OverflowError,sqlite3.IntegrityError) as e:
            self.respond(400,{'error':str(e) if not isinstance(e,sqlite3.IntegrityError) else 'Registro duplicado, login em uso ou dados incompatíveis.'})
        except Exception:
            self.respond(500,{'error':'Erro interno. Verifique os logs do servidor.'})
            import traceback
            traceback.print_exc()

    def mutate(self,con,path,v,user):
        office=user['office_id']
        if path=='/api/logout':
            con.execute('DELETE FROM sessions WHERE token=?',(user['token'],))
        elif path=='/api/password':
            if not hmac.compare_digest(password_hash(core.required(v,'current_password',256),user['salt']),user['password_hash']):
                raise ValueError('Senha atual incorreta.')
            salt,digest=core.password_fields(v.get('password'))
            if hmac.compare_digest(password_hash(v['password'],user['salt']),user['password_hash']):
                raise ValueError('Escolha uma senha diferente da atual.')
            con.execute('UPDATE users SET salt=?,password_hash=?,must_change=0 WHERE id=?',(salt,digest,user['id']))
            con.execute('DELETE FROM sessions WHERE user_id=? AND token!=?',(user['id'],user['token']))
        elif path=='/api/users':
            uid=core.create_user(con,office,v)
            return {'ok':True,'id':uid,'message':'Acesso criado. Compartilhe o login e a senha temporária por um canal seguro. A troca será obrigatória no primeiro acesso.'}
        elif path=='/api/user-update':
            target=core.scoped(con,'users',v['id'],user)
            if target['id']==user['id']:
                raise ValueError('Use Minha conta para trocar sua senha. Seu próprio acesso não pode ser desativado aqui.')
            if 'password' in v:
                salt,digest=core.password_fields(v['password'])
                con.execute('UPDATE users SET salt=?,password_hash=?,must_change=1 WHERE id=?',(salt,digest,target['id']))
            else:
                active=core.flag(v,'active')
                admins=con.execute("SELECT COUNT(*) FROM users WHERE office_id=? AND active=1 AND role='admin'",(office,)).fetchone()[0]
                if target['role']=='admin' and target['active'] and not active and admins<=1:
                    raise ValueError('Mantenha pelo menos um administrador ativo.')
                con.execute('UPDATE users SET active=? WHERE id=?',(active,target['id']))
            con.execute('DELETE FROM sessions WHERE user_id=?',(target['id'],))
        elif path in ('/api/clients','/api/client-settings'):
            if path.endswith('clients'):
                email=core.required(v,'email',254)
                if not re.fullmatch(r'[^\s@\r\n]+@[^\s@\r\n]+\.[^\s@\r\n]+',email):
                    raise ValueError('Informe um e-mail válido.')
                regime=core.required(v,'regime',60)
                if regime not in core.REGIMES:
                    raise ValueError('Regime tributário inválido.')
                con.execute('INSERT INTO clients(office_id,name,email,tax_id,regime) VALUES(?,?,?,?,?)',(office,core.required(v,'name'),email,str(v.get('tax_id',''))[:30],regime))
            else:
                target=core.scoped(con,'clients',v['id'],user)
                phone=str(v.get('phone','')).strip().replace('+','')
                if phone and not re.fullmatch(r'[1-9][0-9]{9,14}',phone):
                    raise ValueError('WhatsApp: informe código do país e DDD, somente números.')
                optin=core.flag(v,'whatsapp_opt_in')
                if optin and not phone:
                    raise ValueError('Informe o número autorizado para WhatsApp.')
                con.execute('UPDATE clients SET phone=?,whatsapp_opt_in=?,reminders_enabled=? WHERE id=?',(phone,optin,core.flag(v,'reminders_enabled'),target['id']))
        elif path in ('/api/documents','/api/obligations'):
            client=core.scoped(con,'clients',v['client_id'],user)['id']
            title,due=core.required(v,'title'),valid_date(v['due'])
            if path.endswith('documents'):
                con.execute('INSERT INTO documents(office_id,client_id,title,due) VALUES(?,?,?,?)',(office,client,title,due))
            else:
                con.execute('INSERT INTO obligations(office_id,client_id,title,due,owner) VALUES(?,?,?,?,?)',(office,client,title,due,core.required(v,'owner',100)))
        elif path=='/api/obligation-review':
            row=core.scoped(con,'obligations',v['id'],user)
            con.execute('UPDATE obligations SET due=?,owner=?,deadline_reviewed=1 WHERE id=?',(valid_date(v['due']),core.required(v,'owner',100),row['id']))
        elif path=='/api/status':
            table=v['table']
            if table not in ('documents','obligations') or v['status'] not in ('Pendente','Em andamento','Concluído'):
                raise ValueError('Status inválido.')
            target=core.scoped(con,table,v['id'],user)
            con.execute(f'UPDATE {table} SET status=? WHERE id=?',(v['status'],target['id']))
        elif path=='/api/upload':
            doc=core.scoped(con,'documents',v['id'],user)
            filename=Path(core.required(v,'filename',150)).name
            if Path(filename).suffix.lower() not in ('.pdf','.xml','.csv','.png','.jpg','.jpeg','.xlsx','.ofx','.txt'):
                raise ValueError('Formato não aceito. Use PDF, XML, CSV, imagem, XLSX, OFX ou TXT.')
            raw=base64.b64decode(v['content'],validate=True)
            if not 0<len(raw)<=8*1024*1024:
                raise ValueError('O arquivo deve ter entre 1 byte e 8 MB.')
            folder=DATA/'uploads'
            folder.mkdir(exist_ok=True,mode=0o700)
            storage=secrets.token_hex(24)
            (folder/storage).write_bytes(raw)
            con.execute('INSERT INTO file_versions(office_id,document_id,filename,storage,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?)',(office,doc['id'],filename,storage,user['id'],core.now()))
            con.execute("UPDATE documents SET filename=?,storage=?,status='Em andamento' WHERE id=?",(filename,storage,doc['id']))
        elif path=='/api/restore-version':
            version=core.scoped(con,'file_versions',v['id'],user)
            core.scoped(con,'documents',version['document_id'],user)
            if not (DATA/'uploads'/version['storage']).is_file():
                raise ValueError('O arquivo desta versão não está no armazenamento.')
            con.execute("UPDATE documents SET filename=?,storage=?,status='Em andamento' WHERE id=?",(version['filename'],version['storage'],version['document_id']))
        elif path=='/api/import':
            return {'ok':True,'message':core.import_records(con,v,user,parse_csv)}
        elif path=='/api/match':
            bank=core.scoped(con,'entries',v['bank_id'],user)
            book=core.scoped(con,'entries',v['book_id'],user)
            if bank['kind']!='bank' or book['kind']!='book' or bank['client_id']!=book['client_id'] or bank['cents']!=book['cents']:
                raise ValueError('A conciliação exige mesmo cliente e mesmo valor.')
            con.execute('INSERT INTO matches(office_id,bank_id,book_id) VALUES(?,?,?)',(office,bank['id'],book['id']))
        elif path=='/api/unmatch':
            target=core.scoped(con,'matches',v['id'],user)
            con.execute('DELETE FROM matches WHERE id=?',(target['id'],))
        elif path=='/api/rules':
            c=core.scoped(con,'clients',v['client_id'],user)
            con.execute('INSERT INTO match_rules(office_id,client_id,bank_text,book_text,tolerance) VALUES(?,?,?,?,?)',(office,c['id'],core.required(v,'bank_text',100),core.required(v,'book_text',100),core.integer(v,'tolerance',0,30)))
        elif path=='/api/rule-toggle':
            target=core.scoped(con,'match_rules',v['id'],user)
            con.execute('UPDATE match_rules SET enabled=? WHERE id=?',(core.flag(v,'enabled'),target['id']))
        elif path=='/api/templates':
            regime=core.required(v,'regime')
            if regime not in core.REGIMES:
                raise ValueError('Regime inválido.')
            start=core.required(v,'start_month',7)
            date.fromisoformat(start+'-01')
            con.execute('INSERT INTO templates(office_id,title,regime,due_day,owner,start_month) VALUES(?,?,?,?,?,?)',(office,core.required(v,'title'),regime,core.integer(v,'due_day',1,31),core.required(v,'owner',100),start))
            count=core.generate_recurring(con,office)
            return {'ok':True,'message':f'Modelo salvo. {count} obrigações geradas para revisão.'}
        elif path=='/api/template-toggle':
            target=core.scoped(con,'templates',v['id'],user)
            con.execute('UPDATE templates SET enabled=? WHERE id=?',(core.flag(v,'enabled'),target['id']))
        elif path=='/api/generate':
            count=core.generate_recurring(con,office)
            return {'ok':True,'message':f'{count} obrigações geradas para revisão (mês atual e próximo).'}
        elif path=='/api/settings':
            con.execute('UPDATE reminder_settings SET enabled=?,days_before=?,interval_days=?,email_enabled=?,whatsapp_enabled=? WHERE office_id=?',
                        (core.flag(v,'enabled'),core.integer(v,'days_before',0,30),core.integer(v,'interval_days',1,30),core.flag(v,'email_enabled'),core.flag(v,'whatsapp_enabled'),office))
        elif path=='/api/reminders':
            return {'ok':True,'message':f'{core.queue_reminders(con,office)} lembretes preparados. Confira os canais e a configuração do worker.'}
        else:
            raise ValueError('Ação não encontrada.')
        return {'ok':True}


def worker_loop(stop):
    while not stop.is_set():
        try:
            core.run_tick(DATA)
        except Exception:
            with db() as con:
                con.execute('INSERT INTO worker_status(id,last_run,error) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET last_run=excluded.last_run,error=excluded.error',(core.now(),'Falha na automação. Verifique armazenamento, integrações e configuração do servidor.'))
            import traceback
            traceback.print_exc()
        stop.wait(60)


def main():
    parser=argparse.ArgumentParser(description='ContaSaas · Gestão para escritórios contábeis')
    parser.add_argument('command',nargs='?',default='serve',choices=['serve','setup','demo','remind','worker','backup','restore'])
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--username',default='admin')
    parser.add_argument('--office',default='Meu escritório')
    parser.add_argument('--office-id',type=int,default=1)
    parser.add_argument('--with-worker',action='store_true')
    parser.add_argument('--file',type=Path)
    parser.add_argument('--destination',type=Path)
    args=parser.parse_args()
    if args.command=='restore':
        if not args.file or not args.destination:
            parser.error('Informe --file BACKUP.zip --destination DIRETORIO_VAZIO')
        core.restore_backup(args.file,args.destination)
        print('Backup restaurado. Configure CONTASAAS_DATA para o novo diretório; sessões antigas foram encerradas.')
        return
    init()
    if args.command=='setup':
        import getpass
        setup(os.environ.get('CONTASAAS_PASSWORD') or getpass.getpass('Senha do administrador (mínimo 12 caracteres): '),args.username,args.office,args.office_id)
        print(f'Acesso configurado: {args.username}')
    elif args.command=='demo':
        seed()
        print('Dados fictícios criados.')
    elif args.command=='remind':
        print(f'{send_reminders()} envios aceitos pelos provedores.')
    elif args.command=='backup':
        print(DATA/'backups'/core.create_backup(DATA))
    elif args.command=='worker':
        worker_loop(threading.Event())
    else:
        with db() as con:
            ready=con.execute("SELECT 1 FROM users WHERE active=1 AND role='admin'").fetchone()
        if not ready:
            parser.error('Configure o administrador primeiro: python app.py setup')
        stop=threading.Event()
        if args.with_worker:
            threading.Thread(target=worker_loop,args=(stop,),daemon=True).start()
        print(f'ContaSaas disponível em http://{args.host}:{args.port}')
        try:
            ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
        except KeyboardInterrupt:
            stop.set()


if __name__=='__main__':
    main()
