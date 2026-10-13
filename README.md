# Roblox Seasonal Alerts 🎃

Bot de monitoramento que verifica itens sazonais do catálogo Roblox e envia uma mensagem em um canal do Discord quando um item passa de indisponível para disponível. O projeto vem configurado para o pacote Headless Horseman (bundle ID `23`).

## Como funciona

- Usa o endpoint público do catálogo Roblox para consultar os detalhes do item.
- Verifica a cada 5 minutos pelo GitHub Actions.
- Envia alerta por webhook do Discord.
- Guarda o estado em `state.json` e o salva no repositório para evitar alertas repetidos (por isso `state.json` **não** deve estar no `.gitignore`).
- Só marca a execução como falha após 3 erros consecutivos, evitando e-mails de falhas passageiras da API.
- Não precisa da senha, cookie ou conta Roblox.

**Limitações:** o agendamento do GitHub Actions pode atrasar e não é um serviço de tempo real garantido. A API pública do catálogo é experimental e pode mudar, impor limites ou não retornar todos os campos para todos os itens. O bot alerta apenas quando a resposta da API permite detectar a disponibilidade; confirme sempre no catálogo oficial.

## 1. Criar o webhook do Discord

1. No Discord, abra seu servidor e vá em **Configurações do servidor → Integrações → Webhooks**.
2. Crie um webhook no canal onde quer receber alertas.
3. Copie a URL. Não a publique nem a envie a outras pessoas: ela permite publicar mensagens no canal.

## 2. Colocar o projeto no GitHub

1. Entre em https://github.com/new e crie um repositório (pode ser privado), por exemplo `roblox-seasonal-alerts`.
2. Extraia os arquivos deste ZIP e envie todos ao repositório, incluindo a pasta oculta `.github/workflows/`.
3. No repositório, abra **Settings → Secrets and variables → Actions → New repository secret**.
4. Nome do segredo: `DISCORD_WEBHOOK_URL`. Valor: a URL do webhook do Discord.
5. Abra **Settings → Actions → General → Workflow permissions** e habilite **Read and write permissions**.
6. Na aba **Actions**, habilite os workflows se o GitHub pedir e execute **Monitorar itens sazonais do Roblox → Run workflow** para testar.

O workflow está agendado para cada 5 minutos. Execuções agendadas podem atrasar. Em repositórios públicos, o GitHub pode desativar agendamentos após 60 dias sem atividade no repositório.

## 3. Adicionar outros itens sazonais

Edite `config/items.json` e acrescente outro objeto à lista. Para pacotes, use `"itemType": "Bundle"`; para itens individuais, use `"itemType": "Asset"`. O `id` deve ser o ID numérico correto do catálogo.

Exemplo de item individual:

```json
{
  "name": "Nome do item",
  "itemType": "Asset",
  "id": 123456789,
  "url": "https://www.roblox.com/catalog/123456789/"
}
```

Não remova a vírgula entre objetos JSON, exceto no último. O monitoramento acompanha apenas os itens cadastrados; ele não descobre automaticamente todos os itens sazonais existentes.

## Testar sem esperar o item abrir

Em **Actions → Monitorar itens sazonais do Roblox → Run workflow** há três opções:

- **test_webhook**: envia só uma mensagem de teste ao Discord (valida o segredo e o webhook).
- **dry_run**: consulta o Roblox e mostra o resultado nos logs, sem enviar alertas e sem salvar estado.
- **debug**: imprime a resposta bruta da API, útil para ver quais campos de status o Roblox devolve.

Localmente (precisa de Python 3.10+):

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v          # testes automáticos (sem rede)
python bot.py --dry-run --debug                  # consulta real, sem alertas
DISCORD_WEBHOOK_URL="..." python bot.py --test-webhook
```

## Primeiro teste e alertas

Na primeira execução, se o item já estiver à venda, o bot envia uma mensagem de inicialização informando que o monitoramento começou com o item disponível. Nas execuções seguintes, só alerta em uma transição de indisponível para disponível.

Se o alerta não chegar:
- Confira **Actions** para ver os logs da execução.
- Confirme que o segredo `DISCORD_WEBHOOK_URL` está definido e que o webhook continua válido.
- Verifique se o endpoint do catálogo retornou o item e os campos de status/preço esperados.
- Confirme manualmente o estado na página oficial do item.

## Segurança

- Nunca coloque o webhook diretamente no código nem em um arquivo público.
- Não é necessário fornecer credenciais do Roblox.
- Se o webhook vazar, apague-o e crie outro no Discord.
