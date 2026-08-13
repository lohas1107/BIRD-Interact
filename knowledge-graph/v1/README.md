# Text-to-SQL Knowledge Graph V1

V1 將 BIRD-Interact Lite 的 KB、schema DDL 與 column meaning 映射成可跨題目使用的語意圖。它刻意不建立 `Task`、query、follow-up、gold SQL 或測試案例節點；資料集只是 ontology/schema mapping 的來源。

## 內容與重現方式

```text
v1/
├── generate.py       # 僅使用 Python 標準庫的 deterministic generator
├── schema.cypher     # Neo4j 5 constraints / indexes
├── mapping.cypher    # 由 generate.py 產生，不應手改
├── reset.cypher      # 完整清空所選 database（具破壞性）
├── validate.cypher   # import 後的 provenance / ID / label 檢查
└── load.sh           # 產生並選擇性載入
```

只重建 Cypher（不需要 Neo4j）：

```bash
./load.sh
# 或指定另一份相同格式 snapshot
./load.sh /path/to/bird-interact-lite
```

載入 Neo4j 5.x：

```bash
export NEO4J_URI=neo4j://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD='...'
export NEO4J_DATABASE=neo4j   # 必須是可丟棄的專用 database
export ALLOW_FULL_NEO4J_RESET=YES
./load.sh
```

設定密碼後的 `load.sh` 會直接執行完整 reset → constraints/indexes → import → validation，同樣會清空目標 database；它不提供保留既有資料的模式。

`schema.cypher`、`mapping.cypher` 使用 stable `id` 和 `MERGE`，但 benchmark 的標準流程是每次完整清空再匯入，避免已刪來源或其他 KG 版本殘留。**警告：`reset.cypher` 執行 `MATCH (n) DETACH DELETE n`，會永久刪除所選 Neo4j database 的所有節點與關係，不只 V1 資料。只可對專用 benchmark database 執行。**

完整、可重複的測評初始化流程：

```bash
cypher-shell ... -d "$NEO4J_DATABASE" -f reset.cypher
cypher-shell ... -d "$NEO4J_DATABASE" -f schema.cypher
cypher-shell ... -d "$NEO4J_DATABASE" -f mapping.cypher
cypher-shell ... -d "$NEO4J_DATABASE" -f validate.cypher
```

每個匯入節點都帶 `kg_version='v1'`、`dataset_id='bird-interact-lite'`。`validate.cypher` 應回報 `wrong_provenance=0`、`missing_id=0`，以及六種 label 的非零數量。

## 六種節點

| Label | 意義 | 主要來源 |
|---|---|---|
| `Concept` | domain、pattern、value/rule output 等概念 | KB、generator pattern |
| `LogicalEntity` | 與 physical schema 解耦的實體 | DDL table |
| `LogicalAttribute` | 實體的語意屬性 | DDL column + column meaning |
| `Metric` | 計算型指標 | `calculation_knowledge` |
| `Rule` | threshold、分類、資格、時間或 opaque rule | `domain_knowledge` |
| `PhysicalSchemaItem` | table 或 column | schema DDL |

Stable ID 範例：

```text
domain:alien
entity:alien:signals
attribute:alien:signals:snrratio
metric:alien:kb:0
rule:alien:kb:50
physical:alien:column:signals.snrratio
```

同名 knowledge 不會跨 database 合併。KB 原文保存在 `raw_definition`；無法可靠 grounding 時保留節點並設 `mapping_status='unresolved'`。

## 十種關係

| Relationship | 起點 → 終點 | 用途 |
|---|---|---|
| `HAS_ATTRIBUTE` | entity → attribute | logical data model |
| `APPLIES_TO` | metric/rule → entity/attribute | 適用範圍；只在唯一推導時建立 |
| `DEPENDS_ON` | KB node → KB node | `children_knowledge` 的 dependency 語義 |
| `USES_ATTRIBUTE` | metric → attribute | 公式精確命中的輸入欄位 |
| `PRODUCES` | rule → concept | rule 的語意輸出 |
| `CLASSIFIES` | rule → entity | classification target 可唯一推導時建立 |
| `INSTANCE_OF_PATTERN` | metric/rule/concept → pattern concept | ratio/composite/temporal 等粗粒度 pattern |
| `IMPLEMENTS` | physical item → entity/attribute | physical-to-logical mapping |
| `JOINABLE_VIA` | FK column/table → referenced column/table | physical join edge；table edge 用於 path search，column edge保留精確 endpoint |
| `VALID_IN_DOMAIN` | semantic node → domain concept | namespace/scope |

V1 的 formula、grain、unit、NULL policy 等尚未獨立成節點；原始公式放在 `raw_definition`，pattern 只是可重建的 heuristic，不代表公式已可執行。Column grounding 僅接受同 database 中唯一、大小寫正規化後的 identifier match。

## 查詢方法

### 1. 找 metric 及其 physical columns

```cypher
MATCH (m:Metric)-[:USES_ATTRIBUTE]->(a:LogicalAttribute)
MATCH (p:PhysicalSchemaItem)-[:IMPLEMENTS]->(a)
WHERE toLower(m.name) CONTAINS 'signal-to-noise'
RETURN m.name, m.raw_definition, a.name, p.database, p.table, p.column;
```

以 alien SNQI 為例，應找到 `signals.snrratio` 與 `signals.noisefloordbm`。這能建立 SQL planner 的 compact evidence pack，但 V1 不會擅自補 NULL semantics。

### 2. 展開 rule 的 knowledge dependency

```cypher
MATCH (r:Rule {domain:'alien'})-[:DEPENDS_ON*1..3]->(input)
WHERE toLower(r.name) CONTAINS 'analyzable signal'
RETURN r.name, labels(input), input.name, input.raw_definition;
```

`children_knowledge` 是依賴，不是 taxonomy child；因此使用 `DEPENDS_ON`，不應查成 `IS_A`。

### 3. 求連接所需欄位的 FK path

```cypher
MATCH (s:PhysicalSchemaItem {id:'physical:alien:table:signals'}),
      (o:PhysicalSchemaItem {id:'physical:alien:table:observatories'}),
      p = shortestPath((s)-[:JOINABLE_VIA*..6]-(o))
RETURN [n IN nodes(p) | n.id] AS items,
       [r IN relationships(p) | {
         predicate: r.predicate,
         left_column: r.left_column,
         right_column: r.right_column
       }] AS joins;
```

這會連通 `signals → telescopes → observatories`。關係可反向遍歷以做 path search，但產生 SQL 時應讀取 `predicate` 與左右欄位，不可依 traversal direction 猜 join 條件。先將需要的 attributes grounding 到 physical table，再在 table-level graph 求路徑；column-level edge可用來核對 FK endpoint。

### 4. 找 unresolved knowledge 供人工或第二階段 mapper 處理

```cypher
MATCH (n)
WHERE (n:Metric OR n:Rule OR n:Concept)
  AND n.mapping_status = 'unresolved'
RETURN n.domain, labels(n), n.name, n.raw_definition
ORDER BY n.domain, n.name;
```

### 5. 檢查特定 domain 的圖規模

```cypher
MATCH (n)-[:VALID_IN_DOMAIN]->(:Concept {id:'domain:alien'})
RETURN labels(n) AS labels, count(*) AS count
ORDER BY count DESC;
```

## Mapping 與可信度限制

- DDL parser 只讀取 `CREATE TABLE (...)` 區段，不把 `First 3 rows` 當完整 facts。
- `cross_db` 的 column documentation 歷史 namespace `cross` 會明確 alias 到 `cross_db`。
- DDL/table/column 名稱 canonicalize 為 lowercase；display meaning 仍保留原文。
- `children_knowledge=-1` 表示沒有 dependency；list 中的 ID 必須存在才建 edge。
- `PhysicalSchemaItem-[:IMPLEMENTS]->Logical*` 是 assertion，column documentation 與 DDL 衝突時不應視為經 DB catalog 驗證。
- 每個可解析 FK 會建立一條 column-level 及一條 table-level `JOINABLE_VIA`；兩者共享 predicate，但有不同 stable edge ID。它不是 transitive relation，也不保證 cardinality 或 join cost。
- Pattern 與 entity scope 是 conservative heuristic；評測時應分開計算 retrieval recall、attribute grounding precision 與 join-path recall。
- V1 不建立 row facts，也不執行或納入 Task/solution SQL，因此不會受到 management task 狀態污染。
