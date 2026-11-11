"""ContaSaas: servidor, persistência e lembretes. Python 3.12+, sem dependências."""
import argparse
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import smtplib
import sqlite3
import ssl
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from http import HTTPStatus
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
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / 'contasaas.db', timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init():
    with db() as con:
        con.executescript(SCHEMA)


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def setup(password):
    if len(password) < 12:
        raise ValueError('Use uma senha com pelo menos 12 caracteres.')
    salt = secrets.token_hex(16)
    (DATA / 'auth.json').write_text(json.dumps({'salt': salt, 'hash': password_hash(password, salt)}))
    os.chmod(DATA / 'auth.json', 0o600)
    with db() as con:
        con.execute('DELETE FROM sessions')


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


def clean(value, key, maxlen=200):
    s = str(value.get(key,'')).strip()
    if not s or len(s)>maxlen:
        raise ValueError(f'Preencha {key} (até {maxlen} caracteres).')
    return s


def valid_date(value):
    return date.fromisoformat(str(value)).isoformat()


def client_exists(con, value):
    n = int(value)
    if not con.execute('SELECT id FROM clients WHERE id=?',(n,)).fetchone():
        raise ValueError('Cliente não encontrado.')
    return n


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
    today = date.today()
    limit = str(today+timedelta(days=7))
    count = 0
    con.execute("UPDATE reminders SET status='Cancelado' WHERE status='Na fila' AND day<?",(str(today),))
    for c in con.execute('SELECT * FROM clients').fetchall():
        items = []
        for table,label in [('documents','Documento'),('obligations','Obrigação')]:
            for row in con.execute(f"SELECT title,due FROM {table} WHERE client_id=? AND status!='Concluído' AND due<=?",(c['id'],limit)):
                items.append(f"{label}: {row['title']} — prazo {row['due']}")
        if items:
            body = f"Olá, equipe {c['name']}.\n\nHá pendências que precisam de atenção:\n\n" + '\n'.join(items) + '\n\nPor favor, entre em contato com seu escritório contábil para regularizar as pendências.\nContaSaas'
            count += con.execute('INSERT OR IGNORE INTO reminders(client_id,day,subject,body) VALUES(?,?,?,?)',(c['id'],str(today),'ContaSaas · Pendências e próximos prazos',body)).rowcount
            con.execute("UPDATE reminders SET body=?,status='Na fila' WHERE client_id=? AND day=? AND status IN ('Na fila','Cancelado')",(body,c['id'],str(today)))
        else:
            con.execute("UPDATE reminders SET status='Cancelado' WHERE client_id=? AND day=? AND status='Na fila'",(c['id'],str(today)))
    return count


def send_reminders():
    """Executar periodicamente pelo agendador do sistema; SMTP TLS obrigatório."""
    required = ['SMTP_HOST','SMTP_USER','SMTP_PASSWORD','SMTP_FROM']
    if any(not os.environ.get(k) for k in required):
        raise ValueError('Configure SMTP_HOST, SMTP_USER, SMTP_PASSWORD e SMTP_FROM antes de enviar.')
    with db() as con:
        queue_reminders(con)
        rows = con.execute("SELECT r.*,c.email FROM reminders r JOIN clients c ON c.id=r.client_id WHERE r.status='Na fila'").fetchall()
        for r in rows:
            msg = EmailMessage()
            msg['From'] = os.environ['SMTP_FROM']
            msg['To'] = r['email']
            msg['Subject'] = r['subject']
            msg.set_content(r['body'])
            try:
                with smtplib.SMTP(os.environ['SMTP_HOST'],int(os.environ.get('SMTP_PORT','587')),timeout=20) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.login(os.environ['SMTP_USER'],os.environ['SMTP_PASSWORD'])
                    smtp.send_message(msg)
                con.execute("UPDATE reminders SET status='Enviado',error=NULL WHERE id=?",(r['id'],))
            except (OSError,smtplib.SMTPException):
                con.execute("UPDATE reminders SET error='Falha SMTP; verifique a configuração e execute novamente.' WHERE id=?",(r['id'],))
            con.commit()
    print('Processamento de lembretes concluído. Consulte o histórico no painel.')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Evita registrar tokens, corpos e parâmetros de requisições.
        print(f'{self.address_string()} {self.command} {urlparse(self.path).path}')

    def respond(self, status, data, mime='application/json; charset=utf-8', headers=None):
        body = json.dumps(data, ensure_ascii=False).encode() if mime.startswith('application/json') else data
        self.send_response(status)
        for k,v in {'Content-Type':mime,'Content-Length':str(len(body)),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",**(headers or {})}.items():
            self.send_header(k,v)
        self.end_headers()
        self.wfile.write(body)

    def payload(self):
        n = int(self.headers.get('Content-Length','0'))
        if not 0 < n <= MAX_BODY:
            raise ValueError('Requisição vazia ou maior que 12 MB.')
        if not self.headers.get('Content-Type','').startswith('application/json'):
            raise ValueError('Use JSON na requisição.')
        data = json.loads(self.rfile.read(n))
        if not isinstance(data,dict):
            raise ValueError('Objeto JSON esperado.')
        return data

    def session(self, con):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie',''))
            token = cookie['session'].value
        except (KeyError,ValueError):
            return None
        return con.execute('SELECT * FROM sessions WHERE token=? AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            static = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if path in static:
                p = ROOT/'static'/static[path]
                mime = {'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[p.suffix[1:]]
                return self.respond(200,p.read_bytes(),mime)
            with db() as con:
                sess = self.session(con)
                if not sess:
                    return self.respond(401,{'error':'Entre para acessar o escritório.'})
                if path == '/api/state':
                    data = {table:[dict(row) for row in con.execute(f'SELECT * FROM {table}')] for table in ['clients','documents','obligations','entries','matches','reminders']}
                    for doc in data['documents']:
                        doc.pop('storage',None)
                    data['csrf'] = sess['csrf']
                    return self.respond(200,data)
                if path.startswith('/api/download/'):
                    doc = con.execute('SELECT * FROM documents WHERE id=?',(int(path.rsplit('/',1)[1]),)).fetchone()
                    if not doc or not doc['storage']:
                        return self.respond(404,{'error':'Arquivo não encontrado.'})
                    return self.respond(200,(DATA/'uploads'/doc['storage']).read_bytes(),'application/octet-stream',{'Content-Disposition':"attachment; filename*=UTF-8''"+ __import__('urllib.parse',fromlist=['quote']).quote(doc['filename'])})
            self.respond(404,{'error':'Recurso não encontrado.'})
        except (ValueError,OSError):
            self.respond(400,{'error':'Não foi possível carregar o recurso.'})

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            data = self.payload()
            with db() as con:
                if path == '/api/login':
                    origin = self.headers.get('Origin')
                    if origin and urlparse(origin).netloc != self.headers.get('Host'):
                        return self.respond(403,{'error':'Origem inválida.'})
                    address = self.client_address[0]
                    attempt = con.execute('SELECT * FROM login_attempts WHERE address=? AND reset>?',(address,time.time())).fetchone()
                    if attempt and attempt['attempts']>=10:
                        return self.respond(429,{'error':'Muitas tentativas. Aguarde 15 minutos.'})
                    authfile = DATA/'auth.json'
                    auth = json.loads(authfile.read_text()) if authfile.exists() else None
                    if not auth or not hmac.compare_digest(password_hash(str(data.get('password','')),auth['salt']),auth['hash']):
                        con.execute('INSERT INTO login_attempts VALUES(?,1,?) ON CONFLICT(address) DO UPDATE SET attempts=CASE WHEN reset<? THEN 1 ELSE attempts+1 END,reset=CASE WHEN reset<? THEN ? ELSE reset END',(address,time.time()+900,time.time(),time.time(),time.time()+900))
                        con.commit()
                        return self.respond(401,{'error':'Senha inválida ou acesso ainda não configurado.'})
                    con.execute('DELETE FROM login_attempts WHERE address=?',(address,))
                    token,csrf = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
                    con.execute('DELETE FROM sessions WHERE expires<=?',(time.time(),))
                    con.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,time.time()+28800))
                    con.commit()
                    secure = '; Secure' if os.environ.get('COOKIE_SECURE')=='1' else ''
                    return self.respond(200,{'ok':True},'application/json; charset=utf-8',{'Set-Cookie':f'session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800{secure}'})
                sess = self.session(con)
                if not sess:
                    return self.respond(401,{'error':'Sua sessão expirou. Entre novamente.'})
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),sess['csrf']):
                    return self.respond(403,{'error':'Atualize a página e tente novamente.'})
                result = self.mutate(con,path,data,sess)
                con.commit()
                return self.respond(200,result)
        except (ValueError,TypeError,KeyError,OverflowError,sqlite3.IntegrityError) as e:
            self.respond(400,{'error':str(e) if not isinstance(e,sqlite3.IntegrityError) else 'Dados incompatíveis ou registro já conciliado.'})
        except Exception:
            self.respond(500,{'error':'Erro interno. Verifique os logs do servidor.'})
            import traceback
            traceback.print_exc()

    def mutate(self,con,path,v,sess):
        if path == '/api/logout':
            con.execute('DELETE FROM sessions WHERE token=?',(sess['token'],))
        elif path == '/api/clients':
            email = clean(v,'email',254)
            if '@' not in email or any(x in email for x in '\r\n'):
                raise ValueError('Informe um e-mail válido.')
            con.execute('INSERT INTO clients(name,email,tax_id,regime) VALUES(?,?,?,?)',(clean(v,'name'),email,str(v.get('tax_id',''))[:30],clean(v,'regime',60)))
        elif path in ['/api/documents','/api/obligations']:
            client = client_exists(con,v['client_id'])
            title,due = clean(v,'title'),valid_date(v['due'])
            if path.endswith('documents'):
                con.execute('INSERT INTO documents(client_id,title,due) VALUES(?,?,?)',(client,title,due))
            else:
                con.execute('INSERT INTO obligations(client_id,title,due,owner) VALUES(?,?,?,?)',(client,title,due,clean(v,'owner',100)))
        elif path == '/api/status':
            table = v['table']
            if table not in ['documents','obligations'] or v['status'] not in ['Pendente','Em andamento','Concluído']:
                raise ValueError('Status inválido.')
            if con.execute(f'UPDATE {table} SET status=? WHERE id=?',(v['status'],int(v['id']))).rowcount!=1:
                raise ValueError('Registro não encontrado.')
        elif path == '/api/upload':
            doc = con.execute('SELECT * FROM documents WHERE id=?',(int(v['id']),)).fetchone()
            if not doc:
                raise ValueError('Solicitação não encontrada.')
            filename = Path(clean(v,'filename',150)).name
            if Path(filename).suffix.lower() not in ['.pdf','.xml','.csv','.png','.jpg','.jpeg','.xlsx','.ofx','.txt']:
                raise ValueError('Formato não aceito. Use PDF, XML, CSV, imagem, XLSX, OFX ou TXT.')
            raw = base64.b64decode(v['content'],validate=True)
            if not 0 < len(raw) <= 8*1024*1024:
                raise ValueError('O arquivo deve ter entre 1 byte e 8 MB.')
            folder = DATA/'uploads'
            folder.mkdir(exist_ok=True)
            storage = secrets.token_hex(24)
            (folder/storage).write_bytes(raw)
            con.execute("UPDATE documents SET filename=?,storage=?,status='Em andamento' WHERE id=?",(filename,storage,doc['id']))
            # Exclusão após commit evita apontar para arquivo removido em rollback.
        elif path == '/api/import':
            client = client_exists(con,v['client_id'])
            kind = v['kind']
            if kind not in ['bank','book']:
                raise ValueError('Selecione extrato ou lançamentos contábeis.')
            rows = parse_csv(clean(v,'csv',2_000_000))
            counts,inserted = {},0
            for dt,desc,cents in rows:
                key = (dt,desc,cents)
                counts[key] = counts.get(key,0)+1
                fp = hashlib.sha256(json.dumps([client,kind,*key,counts[key]],ensure_ascii=False).encode()).hexdigest()
                inserted += con.execute('INSERT OR IGNORE INTO entries(client_id,kind,date,description,cents,fingerprint) VALUES(?,?,?,?,?,?)',(client,kind,dt,desc,cents,fp)).rowcount
            return {'ok':True,'message':f'{inserted} lançamentos importados; {len(rows)-inserted} já existentes.'}
        elif path == '/api/match':
            bank = con.execute("SELECT * FROM entries WHERE id=? AND kind='bank'",(int(v['bank_id']),)).fetchone()
            book = con.execute("SELECT * FROM entries WHERE id=? AND kind='book'",(int(v['book_id']),)).fetchone()
            if not bank or not book or bank['client_id']!=book['client_id'] or bank['cents']!=book['cents']:
                raise ValueError('A conciliação exige mesmo cliente e mesmo valor.')
            con.execute('INSERT INTO matches(bank_id,book_id) VALUES(?,?)',(bank['id'],book['id']))
        elif path == '/api/unmatch':
            con.execute('DELETE FROM matches WHERE id=?',(int(v['id']),))
        elif path == '/api/reminders':
            return {'ok':True,'message':f'{queue_reminders(con)} lembretes criados. O agendador SMTP fará o envio quando configurado.'}
        else:
            raise ValueError('Ação não encontrada.')
        return {'ok':True}


def main():
    parser = argparse.ArgumentParser(description='ContaSaas · Gestão para escritórios contábeis')
    parser.add_argument('command',nargs='?',default='serve',choices=['serve','setup','demo','remind'])
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    args = parser.parse_args()
    init()
    if args.command=='setup':
        import getpass
        setup(os.environ.get('CONTASAAS_PASSWORD') or getpass.getpass('Senha do escritório (mínimo 12 caracteres): '))
        print('Acesso configurado.')
    elif args.command=='demo':
        seed()
        print('Dados fictícios criados.')
    elif args.command=='remind':
        send_reminders()
    else:
        if not (DATA/'auth.json').exists():
            parser.error('Configure o acesso primeiro: python app.py setup')
        print(f'ContaSaas disponível em http://{args.host}:{args.port}')
        ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()

if __name__=='__main__':
    main()
