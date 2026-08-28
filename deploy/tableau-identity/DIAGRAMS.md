# tableau-identity — схемы

Диаграммы к сервису per-user привязки учёток Tableau (1:1 через PAT). Рендерятся
как mermaid (GitHub / Obsidian / VS Code с mermaid-плагином). Текстовая дока —
[TABLEAU-IDENTITY.md](../TABLEAU-IDENTITY.md).

---

## 1. Архитектура: где стоит брокер

Гейт регистрирует как MCP-сервер `tableau` **брокер**, а он проксирует в реальный
`tableau-mcp`, подставляя per-user session-token.

```mermaid
flowchart LR
    Claude["LLM-клиент<br/>(Claude Desktop)"]
    GW["ContextForge<br/>gateway"]
    Broker["tableau-identity<br/>(брокер)"]
    MCP["tableau-mcp<br/>(passthrough)"]
    Tableau[("Tableau Server / Cloud")]
    Admin["tableau-identity-admin<br/>(веб-админка 1:1)"]
    Store[("mappings.yml<br/>PAT зашифрованы")]

    Claude -- "Authorization +<br/>X-Upstream-Authorization" --> GW
    GW -- "Authorization<br/>(личность юзера)" --> Broker
    Broker -- "x-tableau-auth:<br/>session-token" --> MCP
    MCP --> Tableau
    Broker -. "signin по PAT" .-> Tableau
    Broker -. "читает ro" .-> Store
    Admin -- "пишет rw,<br/>шифрует PAT" --> Store

    classDef edge fill:#e6f0fa,stroke:#1f5c8f,color:#16202e
    classDef svc fill:#e8f5ec,stroke:#2f7a4d,color:#16202e
    classDef ext fill:#fdecea,stroke:#b23b3b,color:#16202e
    classDef store fill:#f6ecd5,stroke:#9a6b12,color:#16202e
    class GW,Broker edge
    class MCP,Admin svc
    class Tableau ext
    class Store store
```

---

## 2. Поток одного tool-вызова

Хендшейк (`initialize`/`tools/list`) в Tableau не ходит и токена не требует —
пропускается как есть. Гейтинг применяется только к `tools/call`.

```mermaid
sequenceDiagram
    autonumber
    participant C as Claude
    participant G as Gateway
    participant B as tableau-identity
    participant T as Tableau
    participant M as tableau-mcp

    C->>G: tools/call + токен (Authorization + X-Upstream-Authorization)
    Note over G: X-Upstream-Authorization → Authorization<br/>(проброс личности в upstream)
    G->>B: forward (Authorization: Bearer user-JWT)
    B->>B: проверить подпись JWT, взять sub
    B->>B: найти привязку sub → {tableau_username, PAT}
    alt привязки нет
        B-->>C: JSON-RPC error «нет учётки Tableau»
    else есть
        B->>B: session-token из кэша?
        opt кэш пуст / протух
            B->>T: signin(PAT)  «под пер-юзерным локом»
            T-->>B: session-token
        end
        B->>M: запрос + x-tableau-auth: session-token
        M->>T: REST под сессией юзера (RLS)
        T-->>M: данные в правах юзера
        M-->>B: результат (JSON / SSE)
        B-->>C: стриминг ответа
    end
```

---

## 3. Кэш сессий и правило «один PAT = одна сессия»

Docs Tableau: повторный signin тем же PAT убивает предыдущую сессию. Поэтому
токен кэшируется на юзера, а signin обёрнут пер-юзерным локом.

```mermaid
stateDiagram-v2
    [*] --> НетСессии
    НетСессии --> Логинюсь: get_token() промах кэша
    Логинюсь --> Активна: signin(PAT) под asyncio.Lock
    note right of Логинюсь
        двойная проверка под локом:
        сосед мог уже залогиниться
        → одного PAT = один signin
    end note
    Активна --> Активна: get_token() попадание кэша
    Активна --> НетСессии: invalidate() при 401 от tableau-mcp
    Активна --> НетСессии: истёк TTL (< 240 мин)
    НетСессии --> [*]
```

---

## 4. Гейтинг `tools/call` в брокере

Непривязанный юзер получает чистую ошибку — до общего PAT запрос не доходит.

```mermaid
flowchart TD
    A[Запрос в брокер] --> B{метод == tools/call?}
    B -- нет<br/>(initialize / tools/list) --> P[Проксировать без x-tableau-auth]
    B -- да --> C{есть личность<br/>из токена?}
    C -- нет --> E1["JSON-RPC error<br/>-32001: нет личности"]
    C -- да --> D{есть привязка<br/>sub → PAT?}
    D -- нет --> E2["JSON-RPC error<br/>-32001: нет учётки Tableau"]
    D -- да --> S[session-token из кэша/логина]
    S --> X[Проксировать + x-tableau-auth]
    X --> R{upstream 401?}
    R -- да, 1-й раз --> I[invalidate + релогин + retry]
    I --> X
    R -- нет --> OK[Стримить ответ]

    classDef err fill:#fdecea,stroke:#b23b3b,color:#16202e
    classDef ok fill:#e8f5ec,stroke:#2f7a4d,color:#16202e
    class E1,E2 err
    class OK,X,P ok
```

---

## 5. Как личность долетает до брокера (шов с гейтом)

Проверено **исполнением** настоящей функции ContextForge v1.0.4
`compute_passthrough_headers_cached` против реального кода брокера (без стека).

```mermaid
flowchart LR
    subgraph Клиент
      H1["Authorization:<br/>Bearer user-JWT"]
      H2["X-Upstream-Authorization:<br/>Bearer user-JWT"]
    end
    subgraph Гейт
      F["compute_passthrough_headers_cached()<br/>X-Upstream-Authorization → Authorization<br/>(безусловно, перекрывает свой auth)"]
    end
    subgraph Брокер
      EX["extract_user()<br/>проверка подписи → sub"]
    end
    H1 --> F
    H2 --> F
    F -- "Authorization: Bearer user-JWT" --> EX
    EX --> R["sub = alice@corp<br/>→ PAT алисы"]

    classDef ok fill:#e8f5ec,stroke:#2f7a4d,color:#16202e
    class R ok
```

---

## 6. Соответствие ТС

```mermaid
flowchart LR
    subgraph ТС
      T1["5.4.1 auth к Tableau по PAT"]
      T2["5.4.2 1:1 + RLS"]
      T3["5.5.4 секреты защищены"]
      T4["5.6.2 админка сопоставления"]
    end
    subgraph Реализация
      I1["signin персональным PAT"]
      I2["привязка user→PAT,<br/>вызов под личной сессией"]
      I3["Fernet-шифрование PAT,<br/>наружу не отдаётся"]
      I4["tableau-identity-admin<br/>(UI + REST)"]
    end
    T1 --> I1
    T2 --> I2
    T3 --> I3
    T4 --> I4
```
