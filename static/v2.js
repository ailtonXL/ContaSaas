'use strict';
const isAdmin=()=>state.user.role==='admin';
const isClient=()=>state.user.role==='client';
const regimes=['Simples Nacional','Lucro Presumido','Lucro Real','MEI','Outro'];
const clientOptions=()=>state.clients.map(c=>`<option value="${c.id}">${esc(c.name)}</option>`).join('');
const selectField=(label,name,options)=>`<label>${label}<select name="${name}" aria-label="${label}" required>${options}</select></label>`;
const check=(label,name,checked=false)=>`<label class="checkbox"><input type="checkbox" name="${name}" ${checked?'checked':''}>${label}</label>`;
const roleLabel=r=>({admin:'Administrador',staff:'Equipe',client:'Cliente'}[r]);

function portal(){
  const pending=state.documents.filter(d=>d.status!=='Concluído');
  return title('PORTAL DO CLIENTE',`Olá, ${esc(state.user.name)}.`,`Acompanhe suas pendências com ${esc(state.office.name)}.`)+
    `<section class="hero"><div><span class="eyebrow">SEUS DOCUMENTOS, SEM COMPLICAÇÃO</span><h2>${pending.length?`${pending.length} solicitações para acompanhar.`:'Seus documentos estão em dia.'}</h2><p>Envie os arquivos por aqui. A equipe do escritório fará a revisão.<br>Você pode consultar as versões enviadas a qualquer momento.</p></div><button class="primary hero-button" data-page="documents">Ver meus documentos →</button></section>`+
    `<section class="card">${heading('Seus documentos','Arquivos disponíveis apenas para você e a equipe do escritório.')}${docTable(state.documents,true)}</section>`+
    clientObligations();
}
function clientObligations(){
  return `<section class="card">${heading('Obrigações da sua empresa','Acompanhe o andamento das entregas do escritório.')}<div class="table-wrap"><table><thead><tr><th>OBRIGAÇÃO</th><th>PRAZO</th><th>SITUAÇÃO</th></tr></thead><tbody>${state.obligations.map(o=>`<tr><td>${esc(o.title)}</td><td>${dateBR(o.due)} / ${o.due.slice(0,4)}</td><td>${badge(o)}</td></tr>`).join('')}</tbody></table></div>${!state.obligations.length?empty('Nenhuma obrigação cadastrada para sua empresa.'):''}</section>`;
}
function usersPage(){
  return title('PESSOAS E PERMISSÕES','Cada pessoa, seu acesso.','Cadastre equipe e clientes. A senha temporária deve ser trocada no primeiro acesso.','<button class="primary" data-new="user">+ Novo acesso</button>')+
  `<div class="notice">Administrador: usuários e automações. Equipe: rotinas contábeis. Cliente: apenas seus documentos e obrigações. O login é exclusivo nesta instalação. Portal: <a href="/portal">${esc(location.origin)}/portal</a>.</div><section class="card"><div class="table-wrap"><table><thead><tr><th>PESSOA</th><th>LOGIN</th><th>PERFIL</th><th>CLIENTE</th><th>STATUS</th><th>AÇÕES</th></tr></thead><tbody>${state.users.map(u=>`<tr><td><strong>${esc(u.name)}</strong></td><td>${esc(u.username)}</td><td>${roleLabel(u.role)}</td><td>${u.client_id?esc(client(u.client_id).name):'—'}</td><td><span class="pill ${u.active?'success':'danger'}">${u.active?'Ativo':'Desativado'}</span>${u.must_change?' <small>Troca de senha pendente</small>':''}</td><td>${u.id!==state.user.id?`<button class="text-btn" data-user-toggle="${u.id}" data-active="${u.active?0:1}">${u.active?'Desativar':'Reativar'}</button><button class="text-btn" data-reset-user="${u.id}">Redefinir senha</button>`:'Seu acesso'}</td></tr>`).join('')}</tbody></table></div></section>`;
}
function accountPage(){
  return title('MINHA CONTA',state.user.must_change?'Defina sua nova senha.':'Seu acesso, sob seu controle.',state.user.must_change?'Troque a senha temporária para começar a usar o ContaSaas.':'Ao trocar a senha, suas outras sessões serão encerradas.')+
    `<section class="card account-card">${heading(esc(state.user.name),`${esc(state.user.username)} · ${roleLabel(state.user.role)}`)}<form id="password-form" class="settings-form">${field('Senha atual','current_password','password','autocomplete="current-password" maxlength="256"')}${field('Nova senha','password','password','minlength="12" maxlength="256" autocomplete="new-password"')}${field('Confirme a nova senha','confirmation','password','minlength="12" maxlength="256" autocomplete="new-password"')}<div class="form-actions"><button class="primary">Atualizar minha senha</button></div><p class="error" role="alert"></p></form></section>`;
}
function templatesPage(){
  return title('OBRIGAÇÕES RECORRENTES','Uma rotina que se renova.','Gere obrigações mensais por regime tributário e revise os prazos antes de trabalhar.','<button class="primary" data-new="template">+ Nova recorrência</button>')+
    `<div class="notice">O worker gera o mês atual e o próximo. Datas são propostas a partir do dia cadastrado, limitadas ao último dia do mês. Feriados e regras fiscais precisam da revisão do contador; nenhuma obrigação é transmitida automaticamente.</div><div class="toolbar"><button class="secondary" id="generate-recurring">↻ Gerar agora</button></div><section class="card"><div class="table-wrap"><table><thead><tr><th>OBRIGAÇÃO</th><th>REGIME</th><th>DIA</th><th>RESPONSÁVEL</th><th>INÍCIO</th><th>AÇÃO</th></tr></thead><tbody>${state.templates.map(t=>`<tr><td><strong>${esc(t.title)}</strong><small>${t.enabled?'Ativa':'Pausada'}</small></td><td>${esc(t.regime)}</td><td>${t.due_day}</td><td>${esc(t.owner)}</td><td>${esc(t.start_month)}</td><td><button class="text-btn" data-template-toggle="${t.id}" data-enabled="${t.enabled?0:1}">${t.enabled?'Pausar':'Ativar'}</button></td></tr>`).join('')}</tbody></table></div>${!state.templates.length?empty('Crie um modelo para o regime tributário dos seus clientes.'):''}</section>`;
}
function settingsPage(){
  const s=state.settings;
  return title('AUTOMAÇÕES','O próximo passo, no tempo certo.','Escolha os canais, a antecedência e a frequência dos lembretes.')+
    `<div class="grid"><section class="card">${heading('Lembretes automáticos','O envio respeita os canais e a autorização de cada cliente.')}<form id="settings-form" class="settings-form">${check('Ativar envio automático para este escritório','enabled',s.enabled)}${field('Avisar com quantos dias de antecedência?','days_before','number',`min="0" max="30" value="${s.days_before}"`)}${field('Repetir a cada quantos dias?','interval_days','number',`min="1" max="30" value="${s.interval_days}"`)}${check('Enviar por e-mail','email_enabled',s.email_enabled)}${check('Enviar por WhatsApp, com autorização do cliente','whatsapp_enabled',s.whatsapp_enabled)}<div class="form-actions"><button class="primary">Salvar automação</button></div></form></section><section class="card">${heading('Integrações e operação','Credenciais são configuradas no servidor, nunca neste formulário.')}<div class="settings-form"><p class="hint">E-mail SMTP: <span class="pill ${state.integrations.email?'success':'warning'}">${state.integrations.email?'Configurado':'Não configurado'}</span></p><p class="hint">WhatsApp Cloud API: <span class="pill ${state.integrations.whatsapp?'success':'warning'}">${state.integrations.whatsapp?'Configurado':'Não configurado'}</span></p><p class="hint">Use um template aprovado do WhatsApp com os parâmetros nome e resumo. Confirme o consentimento do cliente na carteira de clientes.</p><div id="operation-status" class="notice">Carregando situação do worker e dos backups…</div><p class="hint">O worker mantém as recorrências, processa lembretes e cria backups diários. A restauração completa é feita pelo operador do servidor em um diretório vazio.</p></div></section></div>`;
}
function auditPage(){
  return title('HISTÓRICO DO ESCRITÓRIO','Cada ação tem uma história.','Últimas 200 ações registradas, com pessoa, horário UTC e recurso alterado.')+
    `<section class="card"><div class="table-wrap"><table><thead><tr><th>DATA / HORA UTC</th><th>PESSOA</th><th>AÇÃO</th><th>RECURSO</th><th>ALTERAÇÃO</th></tr></thead><tbody>${state.audit.map(a=>`<tr><td>${esc(a.created_at)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td>${esc(a.target)}</td><td>${esc(a.detail)}</td></tr>`).join('')}</tbody></table></div>${!state.audit.length?empty('As próximas ações aparecerão aqui.'):''}</section>`;
}
const originalReconciliation=reconciliation;
reconciliation=function(){return originalReconciliation()+`<section class="card">${heading('Regras de correspondência','Sugestões por texto e tolerância de data, sempre com confirmação humana.')}<div class="settings-form"><button class="secondary" data-new="rule">+ Nova regra</button></div><div class="table-wrap"><table><thead><tr><th>CLIENTE</th><th>TEXTO NO EXTRATO</th><th>TEXTO CONTÁBIL</th><th>TOLERÂNCIA</th><th>AÇÃO</th></tr></thead><tbody>${state.match_rules.map(r=>`<tr><td>${esc(client(r.client_id).name)}</td><td>${esc(r.bank_text)}</td><td>${esc(r.book_text)}</td><td>${r.tolerance} dias · ${r.enabled?'Ativa':'Pausada'}</td><td><button class="text-btn" data-rule-toggle="${r.id}" data-enabled="${r.enabled?0:1}">${r.enabled?'Pausar':'Ativar'}</button></td></tr>`).join('')}</tbody></table></div></section>`};
const originalObligations=obligations;
obligations=function(){if(isClient())return title('SUA EMPRESA','Obrigações em acompanhamento.','Consulte os prazos e o andamento das entregas.')+clientObligations();let result=originalObligations();const unreviewed=state.obligations.filter(o=>!o.deadline_reviewed);if(unreviewed.length)result+=`<section class="card">${heading('Revisão de prazos recorrentes',`${unreviewed.length} datas propostas aguardando validação.`)}${unreviewed.map(o=>`<div class="deadline"><div><h3>${esc(o.title)} · ${esc(client(o.client_id).name)}</h3><p>Data proposta: ${o.due} · Competência: ${esc(o.period)}</p></div><button class="secondary" data-review="${o.id}">Revisar prazo</button></div>`).join('')}</section>`;return result};
const originalClients=clients;
clients=function(){return originalClients()+`<section class="card">${heading('Comunicação por cliente','Defina os canais autorizados e pause os lembretes quando necessário.')}<div class="table-wrap"><table><thead><tr><th>CLIENTE</th><th>LEMBRETES</th><th>WHATSAPP</th><th>AÇÃO</th></tr></thead><tbody>${state.clients.map(c=>`<tr><td>${esc(c.name)}</td><td>${c.reminders_enabled?'Ativos':'Pausados'}</td><td>${esc(c.phone)||'Não informado'}<small>${c.whatsapp_opt_in?'Autorizado pelo cliente':'Sem autorização'}</small></td><td><button class="text-btn" data-client-settings="${c.id}">Configurar comunicação</button></td></tr>`).join('')}</tbody></table></div></section>`};
reminders=function(){return title('COMUNICAÇÃO ORGANIZADA','Lembretes com acompanhamento.','Pendências resolvidas deixam de ser cobradas na próxima atualização da fila.','<button class="primary" id="queue-reminders">+ Preparar lembretes</button>')+`<div class="notice">Cada canal tem seu próprio resultado. “Enviado” significa aceito pelo provedor; não é confirmação de leitura. O envio automático requer worker ativo, credenciais e canais habilitados.</div>`+(state.reminders.slice().reverse().map(r=>`<section class="card">${heading(esc(client(r.client_id).name),`${r.day} · ${esc(r.status)}`)}<div class="delivery-status"><span class="pill">E-mail: ${esc(r.email_status)}</span><span class="pill">WhatsApp: ${esc(r.whatsapp_status)}</span></div><pre class="mail-body">${esc(r.body)}</pre>${r.error?`<p class="error settings-form">${esc(r.error)}</p>`:''}</section>`).join('')||`<section class="card">${empty('Prepare os lembretes para acompanhar os próximos envios.')}</section>`)};

function renderV2(){
  if(state.user.must_change)page='account';
  else if(isClient()&&!['portal','documents','obligations','account'].includes(page))page='portal';
  else if(!isAdmin()&&['users','settings','audit'].includes(page))page='overview';
  const allowed=isClient()?['portal','documents','obligations','account']:isAdmin()?Object.keys(names).filter(k=>k!=='portal'):['overview','documents','reconciliation','obligations','clients','reminders','templates','account'];
  document.querySelectorAll('nav button').forEach(b=>{b.hidden=!allowed.includes(b.dataset.page);b.disabled=Boolean(state.user.must_change)&&b.dataset.page!=='account';b.classList.toggle('active',b.dataset.page===page)});
  $('#breadcrumb').innerHTML=`${esc(state.office.name)} <b>/</b> ${names[page]}`;
  $('#doc-count').textContent=state.documents.filter(d=>d.status!=='Concluído').length;
  $('.office>div').innerHTML=`${esc(state.office.name)}<small>${isClient()?'Portal do cliente':'Área de trabalho'}</small>`;
  $('.profile>div').innerHTML=`${esc(state.user.name)}<small>${roleLabel(state.user.role)}</small>`;
  $('#content').innerHTML=({overview,portal,documents,reconciliation,obligations,clients,reminders,templates:templatesPage,users:usersPage,settings:settingsPage,audit:auditPage,account:accountPage}[page])();
  if(isClient())document.querySelectorAll('#content .card').forEach(card=>{if(card.querySelector('[data-upload]'))card.classList.add('portal-documents')});
  if(page==='settings')api('operations').then(data=>{if(page!=='settings')return;$('#operation-status').textContent=`Worker: ${data.worker?.last_run||'ainda não executado'}. Último sucesso: ${data.worker?.last_success||'—'}. ${data.worker?.error||''} Backups locais: ${data.backup_count}. Último: ${data.last_backup||'ainda não criado'}.`}).catch(e=>toast(e.message));
}
const originalOpenForm=openForm;
openForm=function(type,extra={}){
  const clientSelect=selectField('Cliente','client_id','<option value="">Selecione um cliente</option>'+clientOptions());
  const newForms={
    user:['Novo acesso',field('Nome da pessoa','name','text','maxlength="200"')+field('Login individual','username','text','minlength="3" maxlength="100" autocomplete="off"')+selectField('Perfil','role','<option value="staff">Equipe</option><option value="client">Cliente</option><option value="admin">Administrador</option>')+`<div id="user-client" hidden>${clientSelect.replace(' required','')}</div>`+field('Senha temporária','password','password','minlength="12" maxlength="256" autocomplete="new-password"')],
    resetUser:['Redefinir senha',`<input type="hidden" name="id" value="${esc(extra.id)}">`+field('Nova senha temporária','password','password','minlength="12" maxlength="256" autocomplete="new-password"')+'<p class="hint">As sessões deste usuário serão encerradas. Ele precisará trocar a senha ao entrar novamente.</p>'],
    import:['Importar CSV ou OFX',clientSelect+selectField('Origem dos dados','kind','<option value="bank">Extrato bancário</option><option value="book">Lançamentos contábeis</option>')+selectField('Formato','format','<option value="csv">CSV</option><option value="ofx">OFX bancário</option>')+'<label>Arquivo CSV ou OFX<input name="file" type="file" accept=".csv,.ofx" required></label><p class="hint">Até 2 MB e 5.000 lançamentos. CSV UTF-8: data;descricao;valor. OFX: transações STMTTRN com FITID. Reimportações da mesma origem são identificadas.</p><button type="button" class="text-btn" id="sample-csv">↓ Baixar modelo de CSV</button>'],
    rule:['Nova regra de conciliação',clientSelect+field('Texto contido no extrato','bank_text','text','maxlength="100"')+field('Texto contido na contabilidade','book_text','text','maxlength="100"')+field('Tolerância de dias entre lançamentos','tolerance','number','min="0" max="30" value="3"')+'<p class="hint">A regra exige o mesmo cliente e valor. Não confirma conciliações automaticamente.</p>'],
    template:['Nova obrigação recorrente',field('Nome da obrigação','title','text','maxlength="200"')+selectField('Regime tributário','regime',regimes.map(r=>`<option>${r}</option>`).join(''))+field('Dia de vencimento proposto','due_day','number','min="1" max="31" value="20"')+field('Responsável','owner','text','maxlength="100"')+field('Primeiro mês','start_month','month',`value="${isoToday().slice(0,7)}"`)+'<p class="hint">Gera para todos os clientes deste regime. Cada prazo gerado precisa de revisão do contador.</p>'],
    review:['Revisar prazo',`<input type="hidden" name="id" value="${esc(extra.id)}">`+field('Vencimento confirmado','due','date',`value="${esc(extra.due)}"`)+field('Responsável','owner','text',`value="${esc(extra.owner)}" maxlength="100"`)+'<p class="hint">Confirme a data aplicável considerando feriados e regras fiscais.</p>'],
    clientSettings:['Comunicação do cliente',`<input type="hidden" name="id" value="${esc(extra.id)}"><label>WhatsApp com código do país e DDD<input name="phone" maxlength="15" placeholder="5511999999999" value="${esc(extra.phone||'')}"></label>`+check('O cliente autorizou os lembretes por WhatsApp','whatsapp_opt_in',extra.whatsapp_opt_in)+check('Permitir lembretes para este cliente','reminders_enabled',extra.reminders_enabled)]
  };
  if(!newForms[type])return originalOpenForm(type);
  if(['import','rule'].includes(type)&&!state.clients.length){toast('Cadastre um cliente primeiro.');return originalOpenForm('client')}
  $('#modal-title').textContent=newForms[type][0];
  $('#modal-form').dataset.type=type;
  $('#modal-form').innerHTML=newForms[type][1]+'<div class="form-actions"><button type="button" class="secondary" data-close>Cancelar</button><button class="primary">'+(type==='import'?'Importar arquivo':'Salvar')+'</button></div><p class="error" id="form-error" role="alert"></p>';
  $('#modal').showModal();
};
function formValues(form){const values=Object.fromEntries(new FormData(form));form.querySelectorAll('input[type=checkbox]').forEach(i=>values[i.name]=i.checked);return values}
async function submitModal(e){
  e.preventDefault();const form=e.target,type=form.dataset.type,button=form.querySelector('button.primary'),values=formValues(form);button.disabled=true;
  try{
    if(type==='import'){
      const file=values.file;
      if(file.size>2_000_000)throw new Error('O arquivo deve ter até 2 MB.');
      const bytes=await file.arrayBuffer();let text=new TextDecoder('utf-8').decode(bytes);
      if(text.includes('\uFFFD')&&values.format==='ofx')text=new TextDecoder('windows-1252').decode(bytes);
      values[values.format==='csv'?'csv':'content']=text;delete values.file;
    }
    if(type==='user'&&values.role==='client'&&!values.client_id)throw new Error('Selecione o cliente deste acesso.');
    const route={client:'clients',document:'documents',obligation:'obligations',import:'import',user:'users',resetUser:'user-update',rule:'rules',template:'templates',review:'obligation-review',clientSettings:'client-settings'}[type];
    const result=await api(route,values);$('#modal').close();await load();toast(result.message||'Salvo com sucesso.');
  }catch(error){$('#form-error').textContent=error.message}finally{button.disabled=false}
}
function showHistory(id){const doc=state.documents.find(d=>d.id===Number(id));const versions=state.file_versions.filter(v=>v.document_id===Number(id)).reverse();$('#modal-title').textContent=`Versões · ${doc.title}`;$('#modal-form').dataset.type='history';$('#modal-form').innerHTML=versions.map(v=>`<div class="version-row"><div><strong>${esc(v.filename)}</strong><p class="hint">${esc(v.uploaded_at)} · ${esc(state.users.find(u=>u.id===v.uploaded_by)?.name||'Envio autenticado')}</p></div><a class="text-btn" href="/api/version/${v.id}">Baixar</a>${!isClient()?`<button type="button" class="text-btn" data-restore-version="${v.id}">Restaurar</button>`:''}</div>`).join('')||empty('Nenhum arquivo enviado para esta solicitação.');$('#modal').showModal()}
document.addEventListener('change',e=>{if(e.target.name==='role'&&$('#user-client')){$('#user-client').hidden=e.target.value!=='client';$('#user-client select').required=e.target.value==='client'}});
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;
  if(b.dataset.history)showHistory(b.dataset.history);
  else if(b.dataset.restoreVersion){if(await act('restore-version',{id:b.dataset.restoreVersion}))$('#modal').close()}
  else if(b.dataset.resetUser)openForm('resetUser',{id:b.dataset.resetUser});
  else if(b.dataset.userToggle)await act('user-update',{id:b.dataset.userToggle,active:b.dataset.active});
  else if(b.dataset.ruleToggle)await act('rule-toggle',{id:b.dataset.ruleToggle,enabled:b.dataset.enabled});
  else if(b.dataset.templateToggle)await act('template-toggle',{id:b.dataset.templateToggle,enabled:b.dataset.enabled});
  else if(b.dataset.review)openForm('review',state.obligations.find(o=>o.id===Number(b.dataset.review)));
  else if(b.dataset.clientSettings)openForm('clientSettings',state.clients.find(c=>c.id===Number(b.dataset.clientSettings)));
  else if(b.id==='generate-recurring')await act('generate',{});
});
document.addEventListener('submit',async e=>{
  if(!['settings-form','password-form'].includes(e.target.id))return;
  e.preventDefault();const form=e.target,values=formValues(form),button=form.querySelector('button');button.disabled=true;
  try{if(form.id==='password-form'&&values.password!==values.confirmation)throw new Error('A confirmação da senha não confere.');const route=form.id==='settings-form'?'settings':'password';delete values.confirmation;await api(route,values);if(route==='password')page=isClient()?'portal':'overview';await load();toast('Atualizado com sucesso.')}catch(error){toast(error.message)}finally{if(button.isConnected)button.disabled=false}
});
