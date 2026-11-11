"""Regras da versão 2: isolamento, acessos, automações e recuperação."""
import base64
import calendar
from contextlib import closing
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import ssl
import tempfile
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path

ROLES = ('admin', 'staff', 'client')
DOMAIN = ('clients', 'documents', 'obligations', 'entries', 'matches', 'reminders')
REGIMES = ('Simples Nacional', 'Lucro Presumido', 'Lucro Real', 'MEI', 'Outro')


class Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(data):
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(data/'contasaas.db', timeout=30, factory=Connection)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def password_fields(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('A senha deve ter entre 12 e 256 caracteres.')
    salt = secrets.token_hex(16)
    return salt, password_hash(password, salt)


def migrate(con, data):
    """Migração aditiva e idempotente: registros antigos pertencem ao escritório 1."""
    con.executescript('''
    CREATE TABLE IF NOT EXISTS offices(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
    INSERT OR IGNORE INTO offices VALUES(1,'Meu escritório');
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, office_id INTEGER NOT NULL REFERENCES offices(id), username TEXT UNIQUE NOT NULL COLLATE NOCASE, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','staff','client')), client_id INTEGER REFERENCES clients(id), salt TEXT NOT NULL, password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, must_change INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS file_versions(id INTEGER PRIMARY KEY, office_id INTEGER NOT NULL REFERENCES offices(id), document_id INTEGER NOT NULL REFERENCES documents(id), filename TEXT NOT NULL, storage TEXT NOT NULL UNIQUE, uploaded_by INTEGER REFERENCES users(id), uploaded_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, office_id INTEGER NOT NULL REFERENCES offices(id), user_id INTEGER REFERENCES users(id), actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS match_rules(id INTEGER PRIMARY KEY, office_id INTEGER NOT NULL REFERENCES offices(id), client_id INTEGER NOT NULL REFERENCES clients(id), bank_text TEXT NOT NULL, book_text TEXT NOT NULL, tolerance INTEGER NOT NULL DEFAULT 3, enabled INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS templates(id INTEGER PRIMARY KEY, office_id INTEGER NOT NULL REFERENCES offices(id), title TEXT NOT NULL, regime TEXT NOT NULL, due_day INTEGER NOT NULL, owner TEXT NOT NULL, start_month TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS reminder_settings(office_id INTEGER PRIMARY KEY REFERENCES offices(id), enabled INTEGER NOT NULL DEFAULT 0, days_before INTEGER NOT NULL DEFAULT 7, interval_days INTEGER NOT NULL DEFAULT 1, email_enabled INTEGER NOT NULL DEFAULT 1, whatsapp_enabled INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY, expires REAL NOT NULL, token TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS worker_status(id INTEGER PRIMARY KEY CHECK(id=1), last_run TEXT, last_success TEXT, error TEXT);
    ''')
    changes = {t: {'office_id':'INTEGER NOT NULL DEFAULT 1 REFERENCES offices(id)'} for t in DOMAIN}
    changes['sessions'] = {'user_id':'INTEGER REFERENCES users(id)'}
    changes['clients'].update({'phone':"TEXT NOT NULL DEFAULT ''", 'whatsapp_opt_in':'INTEGER NOT NULL DEFAULT 0', 'reminders_enabled':'INTEGER NOT NULL DEFAULT 1'})
    changes['obligations'].update({'template_id':'INTEGER REFERENCES templates(id)', 'period':'TEXT', 'deadline_reviewed':'INTEGER NOT NULL DEFAULT 1'})
    changes['reminders'].update({'email_status':"TEXT NOT NULL DEFAULT 'Na fila'",'whatsapp_status':"TEXT NOT NULL DEFAULT 'Desativado'",'last_attempt':'REAL NOT NULL DEFAULT 0','attempts':'INTEGER NOT NULL DEFAULT 0'})
    for table, fields in changes.items():
        columns = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
        for name, definition in fields.items():
            if name not in columns:
                con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    con.execute('CREATE UNIQUE INDEX IF NOT EXISTS recurring_period ON obligations(template_id,client_id,period)')
    for table in DOMAIN + ('users','file_versions','audit','templates','match_rules'):
        con.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_office ON {table}(office_id)')
    con.execute('INSERT OR IGNORE INTO reminder_settings(office_id) SELECT id FROM offices')
    auth = data/'auth.json'
    if auth.exists() and not con.execute('SELECT 1 FROM users').fetchone():
        old = json.loads(auth.read_text())
        con.execute("INSERT INTO users(office_id,username,name,role,salt,password_hash,must_change) VALUES(1,'admin','Administrador','admin',?,?,0)",(old['salt'],old['hash']))
        con.execute('DELETE FROM sessions')
    con.execute('INSERT OR IGNORE INTO file_versions(office_id,document_id,filename,storage,uploaded_at) SELECT office_id,id,filename,storage,? FROM documents WHERE storage IS NOT NULL',(now(),))
    con.execute("UPDATE reminders SET email_status=status WHERE status IN ('Enviado','Cancelado') AND email_status='Na fila'")
    con.execute('PRAGMA user_version=2')
    con.commit()


def audit(con, user, action, target, detail=''):
    con.execute('INSERT INTO audit(office_id,user_id,actor,action,target,detail,created_at) VALUES(?,?,?,?,?,?,?)',
                (user['office_id'], user['id'], user['name'], action, str(target), str(detail)[:500], now()))


def required(v, key, maximum=200):
    s = str(v.get(key, '')).strip()
    if not s or len(s) > maximum:
        raise ValueError(f'Preencha {key} (até {maximum} caracteres).')
    return s


def integer(v, key, low, high):
    n = int(v[key])
    if not low <= n <= high:
        raise ValueError(f'{key} deve estar entre {low} e {high}.')
    return n


def flag(v, key):
    value = v.get(key, False)
    if value not in (True, False, 0, 1, '0', '1'):
        raise ValueError(f'{key} inválido.')
    return int(value in (True, 1, '1'))


def scoped(con, table, key, user):
    if table not in DOMAIN + ('users','file_versions','match_rules','templates'):
        raise ValueError('Recurso inválido.')
    row = con.execute(f'SELECT * FROM {table} WHERE id=? AND office_id=?',(int(key),user['office_id'])).fetchone()
    if not row:
        raise PermissionError('Registro não disponível para este acesso.')
    if user['role'] == 'client':
        client_id = row['id'] if table == 'clients' else row['client_id'] if table in ('documents','obligations','reminders') else None
        if client_id != user['client_id']:
            raise PermissionError('Registro não disponível para este acesso.')
    return row


def create_user(con, office_id, v):
    role = required(v,'role')
    if role not in ROLES:
        raise ValueError('Perfil inválido.')
    username = required(v,'username',100).lower()
    if not re.fullmatch(r'[a-z0-9@._+\-]{3,100}', username):
        raise ValueError('Login: use 3 a 100 letras, números, ponto, @, +, hífen ou sublinhado.')
    client_id = int(v['client_id']) if role == 'client' else None
    if role == 'client' and not con.execute('SELECT 1 FROM clients WHERE id=? AND office_id=?',(client_id,office_id)).fetchone():
        raise ValueError('Selecione um cliente deste escritório.')
    salt, digest = password_fields(v.get('password'))
    return con.execute('INSERT INTO users(office_id,username,name,role,client_id,salt,password_hash) VALUES(?,?,?,?,?,?,?)',
                       (office_id,username,required(v,'name'),role,client_id,salt,digest)).lastrowid


def state_for(con, sess):
    office_id = sess['office_id']
    user = {k:sess[k] for k in ('id','name','username','role','office_id','client_id','must_change')}
    data = {'user':user, 'office':dict(con.execute('SELECT * FROM offices WHERE id=?',(office_id,)).fetchone()), 'csrf':sess['csrf']}
    if sess['must_change']:
        data.update({table:[] for table in DOMAIN+('file_versions','users','audit','match_rules','templates','suggestions')})
        return data
    for table in DOMAIN + ('file_versions','users','audit','match_rules','templates'):
        if table in ('users','audit') and sess['role'] != 'admin' or sess['role']=='client' and table not in ('clients','documents','obligations','file_versions'):
            data[table] = []
            continue
        fields = 'id,office_id,username,name,role,client_id,active,must_change' if table == 'users' else '*'
        sql, params = f'SELECT {fields} FROM {table} WHERE office_id=?',[office_id]
        if sess['role']=='client':
            if table=='clients':
                sql += ' AND id=?'
            elif table=='file_versions':
                sql += ' AND document_id IN (SELECT id FROM documents WHERE client_id=?)'
            else:
                sql += ' AND client_id=?'
            params.append(sess['client_id'])
        if table=='audit':
            sql += ' ORDER BY id DESC LIMIT 200'
        rows = [dict(r) for r in con.execute(sql,params)]
        for row in rows:
            row.pop('storage', None)
            if sess['role']=='client' and table=='obligations':
                row.pop('owner',None)
        data[table] = rows
    data['suggestions'] = suggestions(con,office_id) if sess['role']!='client' else []
    if sess['role']=='admin':
        data['settings'] = dict(con.execute('SELECT * FROM reminder_settings WHERE office_id=?',(office_id,)).fetchone())
        data['integrations'] = {'email': all(os.environ.get(k) for k in ('SMTP_HOST','SMTP_USER','SMTP_PASSWORD','SMTP_FROM')),
                                'whatsapp':all(os.environ.get(k) for k in ('WHATSAPP_TOKEN','WHATSAPP_PHONE_ID','WHATSAPP_TEMPLATE'))}
    return data


def parse_ofx(text):
    if len(text)>2_000_000 or '<!ENTITY' in text.upper() or '<!DOCTYPE' in text.upper():
        raise ValueError('OFX inválido ou maior que 2 MB.')
    def tag(block, name):
        found = re.search(r'<'+name+r'>\s*([^<\r\n]*)',block,re.I)
        return html.unescape(found.group(1).strip()) if found else ''
    if '<OFX' not in text.upper():
        raise ValueError('Arquivo OFX não reconhecido.')
    accounts={value.strip() for value in re.findall(r'<ACCTID>\s*([^<\r\n]*)',text,re.I)}
    if len(accounts)!=1 or not next(iter(accounts)):
        raise ValueError('Importe um OFX de uma única conta, identificada por ACCTID.')
    account = '|'.join(tag(text,n) for n in ('BANKID','ACCTID','ACCTTYPE'))
    blocks=re.findall(r'<STMTTRN>(.*?)</STMTTRN>',text,re.I|re.S)
    if len(blocks)!=len(re.findall(r'<STMTTRN>',text,re.I)):
        raise ValueError('OFX contém transação sem fechamento STMTTRN; nenhum dado foi importado.')
    result=[]
    for block in blocks:
        try:
            rawdate = tag(block,'DTPOSTED')[:8]
            dt = datetime.strptime(rawdate,'%Y%m%d').date().isoformat()
            value = Decimal(tag(block,'TRNAMT'))
            if not value.is_finite() or abs(value)>Decimal('999999999.99') or value!=value.quantize(Decimal('.01')):
                raise ValueError()
            fitid = tag(block,'FITID')
            desc = tag(block,'MEMO') or tag(block,'NAME') or 'Movimentação bancária'
            if not fitid or len(fitid)>200 or len(desc)>300:
                raise ValueError()
            result.append((dt,desc,int(value*100),account,fitid))
        except (ValueError,InvalidOperation):
            raise ValueError('OFX com lançamento inválido: confira data, valor e FITID.')
    if not result or len(result)>5000:
        raise ValueError('OFX precisa conter de 1 a 5.000 transações STMTTRN com fechamento.')
    return result


def import_records(con, v, user, csv_parser):
    c = scoped(con,'clients',v['client_id'],user)
    kind = required(v,'kind')
    if kind not in ('bank','book'):
        raise ValueError('Origem inválida.')
    fmt = v.get('format','csv')
    text = required(v,'csv' if fmt=='csv' else 'content',2_000_000)
    if fmt not in ('csv','ofx') or fmt=='ofx' and kind!='bank':
        raise ValueError('OFX só pode ser usado para extratos bancários.')
    rows = csv_parser(text) if fmt=='csv' else parse_ofx(text)
    counts, inserted = {},0
    for row in rows:
        dt,desc,cents = row[:3]
        if fmt=='ofx':
            identity=[c['id'],kind,'ofx',row[3],row[4]]
        else:
            key = (dt,desc,cents)
            counts[key] = counts.get(key,0)+1
            identity=[c['id'],kind,*key,counts[key]]
        # Preserva os fingerprints da versão anterior para CSV.
        fp = hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()
        existing=con.execute('SELECT * FROM entries WHERE fingerprint=?',(fp,)).fetchone()
        if existing and fmt=='ofx' and (existing['date']!=dt or existing['cents']!=cents):
            raise ValueError('O mesmo FITID do OFX apareceu com data ou valor diferente. Revise o arquivo.')
        inserted += con.execute('INSERT OR IGNORE INTO entries(office_id,client_id,kind,date,description,cents,fingerprint) VALUES(?,?,?,?,?,?,?)',
                    (user['office_id'],c['id'],kind,dt,desc,cents,fp)).rowcount
    return f'{inserted} lançamentos importados; {len(rows)-inserted} já existentes.'


def suggestions(con, office_id):
    used = {n for r in con.execute('SELECT bank_id,book_id FROM matches WHERE office_id=?',(office_id,)) for n in r}
    records = con.execute('SELECT * FROM entries WHERE office_id=?',(office_id,)).fetchall()
    books={}
    for e in records:
        if e['kind']=='book' and e['id'] not in used:
            books.setdefault((e['client_id'],e['cents']),[]).append(e)
    rules=con.execute('SELECT * FROM match_rules WHERE office_id=? AND enabled=1',(office_id,)).fetchall()
    result=[]
    for b in records:
        if b['kind']!='bank' or b['id'] in used:
            continue
        for l in books.get((b['client_id'],b['cents']),[]):
            delta=abs((date.fromisoformat(b['date'])-date.fromisoformat(l['date'])).days)
            reason='Mesma data e valor' if delta==0 else None
            for r in rules:
                if r['client_id']==b['client_id'] and r['bank_text'].casefold() in b['description'].casefold() and r['book_text'].casefold() in l['description'].casefold() and delta<=r['tolerance']:
                    reason=f"Regra #{r['id']} · até {r['tolerance']} dias"
                    break
            if reason:
                result.append({'bank_id':b['id'],'book_id':l['id'],'reason':reason,'distance':delta})
    return sorted(result,key=lambda r:r['distance'])


def generate_recurring(con, office_id=None, on=None):
    current=on or date.today()
    months=[date(current.year,current.month,1)]
    months.append(date(current.year+current.month//12,current.month%12+1,1))
    params=[] if office_id is None else [office_id]
    rules=con.execute('SELECT * FROM templates WHERE enabled=1'+(' AND office_id=?' if office_id else ''),params).fetchall()
    count=0
    for r in rules:
        before=count
        clients=con.execute('SELECT id FROM clients WHERE office_id=? AND regime=?',(r['office_id'],r['regime'])).fetchall()
        for month in months:
            period=month.strftime('%Y-%m')
            if period < r['start_month']:
                continue
            due=date(month.year,month.month,min(r['due_day'],calendar.monthrange(month.year,month.month)[1]))
            for c in clients:
                count+=con.execute('INSERT OR IGNORE INTO obligations(office_id,client_id,title,due,owner,template_id,period,deadline_reviewed) VALUES(?,?,?,?,?,?,?,0)',
                           (r['office_id'],c['id'],r['title'],str(due),r['owner'],r['id'],period)).rowcount
        if count>before:
            audit(con,{'id':None,'name':'Automação','office_id':r['office_id']},'recorrência-gerada',r['id'],f'{count-before} obrigações propostas para revisão')
    return count


def queue_reminders(con, office_id=None, automatic=False, on=None):
    today=on or date.today()
    con.execute("UPDATE reminders SET status='Cancelado',email_status=CASE WHEN email_status='Enviado' THEN email_status ELSE 'Cancelado' END,whatsapp_status=CASE WHEN whatsapp_status='Enviado' THEN whatsapp_status ELSE 'Cancelado' END WHERE status!='Enviado' AND day<?"+(' AND office_id=?' if office_id else ''),[str(today)]+([office_id] if office_id else []))
    count=0
    for settings in con.execute('SELECT * FROM reminder_settings'+(' WHERE office_id=?' if office_id else ''),[office_id] if office_id else []).fetchall():
        oid=settings['office_id']
        if automatic and not settings['enabled']:
            continue
        limit=str(today+timedelta(days=settings['days_before']))
        for c in con.execute('SELECT * FROM clients WHERE office_id=?',(oid,)).fetchall():
            items=[]
            if c['reminders_enabled']:
                for table,label in [('documents','Documento'),('obligations','Obrigação')]:
                    condition="status='Pendente'" if table=='documents' else "status!='Concluído' AND deadline_reviewed=1"
                    for row in con.execute(f"SELECT title,due FROM {table} WHERE office_id=? AND client_id=? AND {condition} AND due<=?",(oid,c['id'],limit)):
                        items.append(f"{label}: {row['title']} — prazo {row['due']}")
            existing=con.execute('SELECT * FROM reminders WHERE client_id=? AND day=?',(c['id'],str(today))).fetchone()
            if not items:
                if existing and existing['status']!='Enviado':
                    con.execute("UPDATE reminders SET status='Cancelado',email_status=CASE WHEN email_status='Enviado' THEN email_status ELSE 'Cancelado' END,whatsapp_status=CASE WHEN whatsapp_status='Enviado' THEN whatsapp_status ELSE 'Cancelado' END WHERE id=?",(existing['id'],))
                continue
            last=con.execute("SELECT MAX(day) FROM reminders WHERE client_id=? AND (email_status='Enviado' OR whatsapp_status='Enviado') AND day<?",(c['id'],str(today))).fetchone()[0]
            if last and (today-date.fromisoformat(last)).days<settings['interval_days']:
                continue
            body=f"Olá, equipe {c['name']}.\n\nHá pendências que precisam de atenção:\n\n"+'\n'.join(items)+'\n\nAcesse seu portal ContaSaas ou entre em contato com o escritório.'
            email='Na fila' if settings['email_enabled'] else 'Desativado'
            whatsapp='Na fila' if settings['whatsapp_enabled'] and c['whatsapp_opt_in'] and c['phone'] else 'Desativado'
            if email==whatsapp=='Desativado':
                continue
            if not existing:
                count+=con.execute('INSERT INTO reminders(office_id,client_id,day,subject,body,email_status,whatsapp_status) VALUES(?,?,?,?,?,?,?)',
                        (oid,c['id'],str(today),'ContaSaas · Pendências e próximos prazos',body,email,whatsapp)).rowcount
            elif existing['status']!='Enviado':
                con.execute("UPDATE reminders SET body=?,status='Na fila',email_status=CASE WHEN email_status='Enviado' THEN email_status ELSE ? END,whatsapp_status=CASE WHEN whatsapp_status='Enviado' THEN whatsapp_status ELSE ? END WHERE id=?",(body,email,whatsapp,existing['id']))
    return count


def acquire_lease(con, name, ttl=600):
    token=secrets.token_hex(16)
    con.execute('DELETE FROM leases WHERE name=? AND expires<?',(name,time.time()))
    try:
        con.execute('INSERT INTO leases VALUES(?,?,?)',(name,time.time()+ttl,token))
        con.commit()
        return token
    except sqlite3.IntegrityError:
        con.rollback()
        return None


def send_email(row):
    if not all(os.environ.get(k) for k in ('SMTP_HOST','SMTP_USER','SMTP_PASSWORD','SMTP_FROM')):
        raise ValueError('SMTP não configurado.')
    msg=EmailMessage()
    msg['From']=os.environ['SMTP_FROM']
    msg['To']=row['email']
    msg['Subject']=row['subject']
    msg.set_content(row['body'])
    with smtplib.SMTP(os.environ['SMTP_HOST'],int(os.environ.get('SMTP_PORT','587')),timeout=20) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(os.environ['SMTP_USER'],os.environ['SMTP_PASSWORD'])
        smtp.send_message(msg)


def send_whatsapp(row):
    if not all(os.environ.get(k) for k in ('WHATSAPP_TOKEN','WHATSAPP_PHONE_ID','WHATSAPP_TEMPLATE')):
        raise ValueError('WhatsApp Cloud API não configurada.')
    phone_id=os.environ['WHATSAPP_PHONE_ID']
    version=os.environ.get('WHATSAPP_API_VERSION','v23.0')
    if not phone_id.isdigit() or not re.fullmatch(r'v\d+\.0',version):
        raise ValueError('Configuração WhatsApp inválida.')
    # Template aprovado deve ter dois parâmetros no corpo: nome e resumo.
    summary='; '.join(row['body'].split('\n')[4:-2])[:900]
    payload={'messaging_product':'whatsapp','to':row['phone'],'type':'template','template':{'name':os.environ['WHATSAPP_TEMPLATE'],'language':{'code':os.environ.get('WHATSAPP_LANGUAGE','pt_BR')},'components':[{'type':'body','parameters':[{'type':'text','text':row['name']},{'type':'text','text':summary}]}]}}
    request=urllib.request.Request(f'https://graph.facebook.com/{version}/{phone_id}/messages',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+os.environ['WHATSAPP_TOKEN'],'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=20) as response:
        result=json.load(response)
        if not result.get('messages'):
            raise ValueError('WhatsApp não confirmou aceitação.')


def send_reminders(data, automatic=False):
    with connect(data) as con:
        token=acquire_lease(con,'reminders',ttl=120)
        if not token:
            return 0
        sent=0
        try:
            queue_reminders(con,automatic=automatic)
            con.commit()
            rows=con.execute("SELECT r.id FROM reminders r JOIN reminder_settings s ON s.office_id=r.office_id WHERE r.status='Na fila' AND (r.error IS NULL OR r.last_attempt<?)"+(' AND s.enabled=1' if automatic else ''),(time.time()-900,)).fetchall()
            for item in rows:
                # Renova o bloqueio e revalida pendências antes de cada destinatário.
                con.execute('UPDATE leases SET expires=? WHERE name=? AND token=?',(time.time()+120,'reminders',token))
                queue_reminders(con,automatic=automatic)
                row=con.execute("SELECT r.*,c.email,c.phone,c.name,c.whatsapp_opt_in,c.reminders_enabled,s.enabled,s.email_enabled,s.whatsapp_enabled FROM reminders r JOIN clients c ON c.id=r.client_id JOIN reminder_settings s ON s.office_id=r.office_id WHERE r.id=?",(item['id'],)).fetchone()
                con.commit()
                if row['status']!='Na fila' or not row['reminders_enabled'] or automatic and not row['enabled']:
                    continue
                con.execute('UPDATE reminders SET last_attempt=?,attempts=attempts+1 WHERE id=?',(time.time(),row['id']))
                con.commit()
                errors=[]
                for channel,sender in [('email',send_email),('whatsapp',send_whatsapp)]:
                    column=channel+'_status'
                    if row[column]!='Na fila' or not row[channel+'_enabled'] or channel=='whatsapp' and not row['whatsapp_opt_in']:
                        continue
                    try:
                        sender(row)
                        con.execute(f"UPDATE reminders SET {column}='Enviado' WHERE id=?",(row['id'],))
                        sent+=1
                    except Exception:
                        # Nunca persistir resposta de provedor que possa conter credenciais ou PII.
                        errors.append(f'{channel}: falha ou integração não configurada; confira o provedor.')
                    con.commit()
                status=con.execute('SELECT email_status,whatsapp_status FROM reminders WHERE id=?',(row['id'],)).fetchone()
                complete=all(s in ('Enviado','Desativado','Cancelado') for s in status)
                con.execute('UPDATE reminders SET status=?,error=? WHERE id=?',('Enviado' if complete else 'Na fila',' '.join(errors) or None,row['id']))
                con.commit()
            return sent
        finally:
            con.execute('DELETE FROM leases WHERE name=? AND token=?',('reminders',token))
            con.commit()


def create_backup(data):
    """Snapshot consistente do banco e anexos referenciados; bloqueia gravações na cópia."""
    folder=data/'backups'
    folder.mkdir(mode=0o700,exist_ok=True)
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+secrets.token_hex(4)+'.zip'
    final=folder/name
    with connect(data) as lock:
        lock.execute('BEGIN IMMEDIATE')
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            dbfile=Path(tmp)/'contasaas.db'
            with connect(data) as source,closing(sqlite3.connect(dbfile)) as destination:
                source.backup(destination)
            paths={'contasaas.db':dbfile}
            for row in lock.execute('SELECT DISTINCT storage FROM file_versions'):
                path=data/'uploads'/row['storage']
                if not path.is_file():
                    raise ValueError('Há anexo ausente; backup não foi criado.')
                paths['uploads/'+row['storage']]=path
            manifest={key:hashlib.sha256(path.read_bytes()).hexdigest() for key,path in paths.items()}
            partial=final.with_suffix('.tmp')
            try:
                with zipfile.ZipFile(partial,'w',zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr('manifest.json',json.dumps({'version':2,'created_at':now(),'sha256':manifest}))
                    for key,path in paths.items():
                        archive.write(path,key)
                os.chmod(partial,0o600)
                partial.rename(final)
            finally:
                partial.unlink(missing_ok=True)
    return name


def restore_backup(archive_path, destination):
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('A restauração exige um diretório de destino vazio, sem servidor em execução.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        extracted=Path(temporary)
        with zipfile.ZipFile(archive_path) as archive:
            names=archive.namelist()
            if len(names)!=len(set(names)) or sum(i.file_size for i in archive.infolist())>2*1024**3:
                raise ValueError('Backup duplicado ou maior que 2 GB descompactado.')
            manifest=json.loads(archive.read('manifest.json'))
            hashes=manifest['sha256']
            if manifest.get('version')!=2 or 'contasaas.db' not in hashes or set(names)!=set(hashes)|{'manifest.json'}:
                raise ValueError('Manifesto do backup inválido.')
            for name,digest in hashes.items():
                if name!='contasaas.db' and not re.fullmatch(r'uploads/[a-f0-9]{48}',name):
                    raise ValueError('Caminho inválido no backup.')
                content=archive.read(name)
                if not hmac.compare_digest(hashlib.sha256(content).hexdigest(),digest):
                    raise ValueError('A integridade do backup não confere.')
                path=extracted/name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(content)
            with closing(sqlite3.connect(extracted/'contasaas.db')) as con:
                if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or con.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Banco do backup inválido.')
                con.execute('DELETE FROM sessions')
                con.execute('DELETE FROM leases')
                con.commit()
            destination.mkdir(exist_ok=True,mode=0o700)
            for path in extracted.iterdir():
                shutil.move(str(path),destination/path.name)


def run_tick(data):
    with connect(data) as con:
        generate_recurring(con)
    send_reminders(data,automatic=True)
    retention=int(os.environ.get('BACKUP_RETENTION_DAYS','30'))
    if retention<1:
        raise ValueError('BACKUP_RETENTION_DAYS deve ser positivo.')
    folder=data/'backups'
    prefix=datetime.now(timezone.utc).strftime('%Y%m%d')
    if not folder.exists() or not list(folder.glob(prefix+'T*.zip')):
        create_backup(data)
    for path in folder.glob('*.zip'):
        if path.stat().st_mtime < time.time()-retention*86400:
            path.unlink()
    with connect(data) as con:
        con.execute('INSERT INTO worker_status(id,last_run,last_success,error) VALUES(1,?,?,NULL) ON CONFLICT(id) DO UPDATE SET last_run=excluded.last_run,last_success=excluded.last_success,error=NULL',(now(),now()))
