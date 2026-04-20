# FB Groups Blaster

Ferramenta local pra **entrar em vários grupos do Facebook de uma vez** a partir de uma palavra-chave, ver o status de cada um (membro / pendente de aprovação / erro) e **postar uma mensagem** apenas nos grupos onde você já foi aceito.

UI web com dashboard em tempo real — sobe um servidor em `http://localhost:5050` e usa automação de browser via AdsPower + Chrome DevTools Protocol.

![ui](docs/preview.png)

## Por que é útil

- Entrar em 50 grupos manualmente dá trabalho. Aqui você digita a palavra-chave, define a quantidade, e ele rola a busca + clica em "Participar" em todos.
- Classifica cada grupo na hora:
  - 🟢 **Membro** (público sem aprovação) → pode postar imediatamente
  - 🟡 **Pendente** → esperando admin aprovar
  - 🔴 **Erro** / Não entrou
- Depois, você seleciona só os verdes e dispara a mensagem em todos, com **delay aleatório** entre posts (padrão 60–180s) pra reduzir chance de flag.

## ⚠️ Aviso

Postar a mesma mensagem em muitos grupos em sequência é padrão clássico de spam. O Facebook detecta rápido e bloqueia/bane. Use com cabeça:

- Comece com 5–10 grupos, não 50
- Varie o texto
- Delays longos (o default já ajuda)
- Grupos segmentados na sua área

Você é responsável pelo uso.

## Requisitos

- **Windows** (os paths são Windows; adaptável pra macOS/Linux trocando OUT_DIR)
- **Python 3.10+** com as libs:
  ```bash
  pip install websocket-client
  ```
- **AdsPower Global** instalado, com um perfil já logado no Facebook
  - Por padrão o script usa o perfil `k17pnv2n`. Troque em `blaster.py` na constante `USER_ID` se for outro.
- AdsPower rodando antes de você clicar em Iniciar

## Como usar

```bash
cd fb-groups-blaster
python server.py
```

Abre o browser em `http://localhost:5050`.

1. **Participar dos grupos** → digite palavra-chave, quantidade, clique Iniciar
2. **Grupos coletados** → revise a tabela, use "Selecionar só aprovados"
3. **Postar mensagem** → escreva o texto, confirme, aguarde

Arquivos de estado ficam em `grupos.json` e `log.json`.

## Uso pela linha de comando (sem UI)

```bash
python blaster.py join --query "contabilidade" --scrolls 50
python blaster.py post --msg-file mensagem.txt
python blaster.py list
```

## Como funciona por baixo

1. `server.py` sobe um HTTP server local (stdlib, sem Flask)
2. Fala com o AdsPower pela API local (`http://local.adspower.net:50325`)
3. Abre o perfil do browser e pega a porta de debug do Chrome
4. Via **CDP WebSocket**, injeta JavaScript que:
   - Scrolla a busca de grupos
   - Clica em todos os botões "Participar" até atingir o limite
   - Classifica o status olhando o DOM após o clique
5. Pra postar, navega em cada URL de grupo e injeta JS que preenche o editor e clica em "Publicar"
6. Frontend faz polling em `/api/state` a cada 1.5s

## Estrutura

```
fb-groups-blaster/
├── server.py      # HTTP + orquestração
├── blaster.py     # CLI + lógica CDP / AdsPower
├── index.html     # UI
└── README.md
```

## Licença

MIT
