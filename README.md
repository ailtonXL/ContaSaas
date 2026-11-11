# ContaSaas

Um escritório contábil mais organizado: documentos, conciliação e obrigações em uma aplicação web em português.

![Painel do ContaSaas com dados fictícios](docs/painel.png)

## O que funciona

- **Documentos:** solicitações por cliente e prazo, anexos de até 8 MB, download autenticado e revisão por status.
- **Conciliação:** importação CSV de extrato e contabilidade, sugestões por cliente/valor/data, confirmação pelo contador, prevenção de dupla conciliação e opção de desfazer.
- **Obrigações:** agenda ordenada por vencimento, responsável, cliente, status e alertas visuais de atraso e proximidade.
- **Lembretes:** fila diária por cliente para pendências com prazo até 7 dias, envio por SMTP com STARTTLS e histórico de sucesso/falha.
- **Painel:** indicadores calculados com os dados salvos, buscas e filtros por cliente.
- **Persistência:** SQLite e arquivos locais; nenhuma conta externa é necessária para iniciar.

## Executar

Requisito: **Python 3.12 ou superior**, sem dependências externas de execução.

```bash
git clone https://github.com/ailtonXL/ContaSaas.git
cd ContaSaas
python3 app.py setup
# Opcional: dados fictícios para explorar, apenas com banco vazio
python3 app.py demo
python3 app.py
```

Acesse **http://127.0.0.1:8000** e use a senha escolhida no setup (mínimo 12 caracteres). Não existe senha padrão. O comando `setup` também permite redefinir a senha e encerra as sessões anteriores. Os dados de demonstração são fictícios e os e-mails usam o domínio reservado `.example`.

Para outro endereço/porta: `python3 app.py serve --host 0.0.0.0 --port 8000`.

## Fluxo de uso

1. Cadastre o cliente, seu e-mail e regime tributário.
2. Crie solicitações de documentos; a equipe do escritório anexa os arquivos recebidos e marca como concluído após revisar.
3. Importe um CSV bancário e outro contábil para o mesmo cliente. Confira e confirme as correspondências.
4. Cadastre obrigações com vencimento e responsável. Atualize os status conforme o trabalho avança.
5. Prepare lembretes no painel ou execute o agendador abaixo para preparar e enviar automaticamente.

### CSV

Arquivo UTF-8, delimitado por ponto e vírgula (recomendado) ou vírgula, com cabeçalho exato:

```csv
data;descricao;valor
2026-11-10;Recebimento cliente;1500,00
2026-11-10;Pagamento fornecedor;-250,00
```

Até 5.000 linhas e 2 MB na interface. Datas ISO, saídas negativas, valores em reais com no máximo duas casas decimais. A importação é atômica: uma linha inválida cancela o lote inteiro. Reimportar o mesmo conteúdo para o mesmo cliente/origem não duplica lançamentos. Ocorrências idênticas dentro do mesmo arquivo são preservadas; arquivos sobrepostos com repetições idênticas devem ser revisados antes da importação.

## Lembretes automáticos

Configure variáveis de ambiente `SMTP_HOST`, `SMTP_PORT` (padrão 587), `SMTP_USER`, `SMTP_PASSWORD` e `SMTP_FROM`. Consulte `.env.example`; o aplicativo **não carrega `.env` automaticamente**. Configure o ambiente no seu serviço/agendador, sem colocar senhas no repositório ou na linha de comando.

```bash
python3 app.py remind
```

Agende esse comando diariamente no cron ou Agendador de Tarefas. Exemplo Linux (o ambiente SMTP deve ser fornecido pelo serviço que executa o comando):

```cron
0 8 * * * cd /caminho/ContaSaas && /usr/bin/python3 app.py remind >> /caminho/contasaas-reminders.log 2>&1
```

O comando cria até um lembrete por cliente/dia e envia itens na fila. Falhas permanecem na fila para nova tentativa no mesmo dia, com erro visível no painel. Antes do envio, a fila do dia é atualizada com as pendências atuais; itens resolvidos e filas de dias anteriores são cancelados para evitar cobranças antigas. Execute **um único processo de envio por vez**. Em caso de interrupção após a aceitação pelo SMTP e antes da gravação no banco, o provedor pode receber uma repetição. A aceitação SMTP não garante entrega na caixa de entrada. Sem SMTP configurado, os itens ficam na fila e nenhum e-mail é enviado.

## Dados e acesso

`data/contasaas.db` guarda o banco; `data/uploads/` guarda anexos; `data/auth.json` guarda o hash scrypt da senha. `CONTASAAS_DATA` muda o diretório de persistência (prefira caminho absoluto em serviços). Esses dados são ignorados pelo Git. Para backup consistente, pare os processos e copie o diretório inteiro; restaure com os mesmos arquivos e permissões. Anexos substituídos são preservados no armazenamento para evitar perda durante falhas, mas apenas o anexo atual fica disponível na interface.

Acesso inicial para **um escritório, com senha compartilhada**. Sessões expiram em 8 horas, cookies HttpOnly/SameSite, proteção CSRF nas alterações, limite de tentativas de login e downloads somente autenticados. Para uso na internet, utilize proxy reverso com HTTPS, `COOKIE_SECURE=1`, armazenamento persistente e backups. O servidor padrão da biblioteca Python é destinado ao uso local e piloto controlado.

## Escopo desta versão

Este é um MVP funcional de uso interno, sem portal para clientes, usuários individuais, isolamento entre escritórios, faturamento SaaS, OCR, integração bancária/Receita/SEFAZ ou transmissão de obrigações. Os prazos são cadastrados pelo contador; os alertas não consultam automaticamente o calendário fiscal. Arquivos são armazenados, sem interpretar ou validar o conteúdo fiscal. Upload/download é feito pela equipe autenticada. A conciliação nunca transmite nem altera dados bancários.

## Validação

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
```

Os testes cobrem login, CSRF, persistência, importação atômica/deduplicação, conciliação, anexos autenticados e fila de lembretes. Teste de navegador opcional: instale Playwright em um ambiente de desenvolvimento, inicie uma instância descartável com dados de demonstração e execute `python3 tests/browser_smoke.py URL`; a senha é lida de `TEST_PASSWORD`.
