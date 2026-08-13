# BIRD-Interact Text-to-SQL KG V2

V2 是可重複測評的、非 Task-centric 語意知識圖。輸入只包含各 database 的
`*_schema.txt`、`*_column_meaning_base.json` 與 `*_kb.jsonl`；不匯入 query、task、
follow-up、gold SQL 或測試案例。

相較最小圖，V2 將公式、資料型別、值域、單位、粒度、聚合、時間、join、assertion、
來源與版本提升成明確節點。只有 deterministic extraction 才建立結構化關係；原始文字
永遠保留，未解析內容明確標示 `raw` 或 `unresolved`。

## 檔案

- `generate_mapping.py`：讀取資料集，重建 `mapping.cypher`。
- `mapping.cypher`：完整、可重跑的 Neo4j 5.x 匯入檔；由生成器產生，勿手改。
- `reset.cypher`：清空整個 Neo4j database 及本 mapping 建立的 schema objects。
- `validate.cypher`：匯入後 cardinality、identity、provenance 與結構檢查。
- `load.sh`：具有安全開關的 reset → generate → import → validate 一鍵流程。

## 警告：reset 會刪除整個 database

`reset.cypher` 會執行：

```cypher
MATCH (n) DETACH DELETE n;
```

這不是只刪 V2 節點，而是不可逆地刪除目前 Neo4j database 中的**所有節點與關係**。
只可對專供測評、可丟棄的 database 執行。執行前務必核對 `NEO4J_DATABASE`，不要指向
共享、開發或正式環境。本設計刻意不考慮 V1/V2 共存：每次測評皆 full reset 後完整匯入。

## 固定重建流程

需求：Python 3.10+、Neo4j 5.x、`cypher-shell`。以下假定當前目錄為本資料夾：

```bash
python3 generate_mapping.py \
  --dataset ../../BIRD-Interact-Claude/bird-interact-lite \
  --output mapping.cypher

# 再次確認這是可丟棄的測評 database。
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
  -d "$NEO4J_DATABASE" -f reset.cypher

# mapping.cypher 開頭會建立 constraints/indexes，然後完整匯入。
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
  -d "$NEO4J_DATABASE" -f mapping.cypher

cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
  -d "$NEO4J_DATABASE" -f validate.cypher
```

不得把密碼提交到 repository。若本機使用預設 database，可明確設
`NEO4J_DATABASE=neo4j`，但仍必須先確認它可完整刪除。

`mapping.cypher` 使用 `MERGE`，意外重跑不應製造重複 identity；正式 benchmark run 仍應
先 full reset，以排除上一輪資料、constraint 或人工修改造成的污染。

也可使用一鍵流程；除非明確給出 `YES`，腳本會拒絕清庫：

```bash
export NEO4J_URI='neo4j://localhost:7687'
export NEO4J_USER='neo4j'
export NEO4J_PASSWORD='...'
export NEO4J_DATABASE='disposable-kg-eval'
export ALLOW_FULL_NEO4J_RESET='YES'
./load.sh
```

## 節點模型

所有節點同時具有 `:KGNode` label 與全域唯一 `id`。

| 節點 | 用途 | 主要屬性 |
|---|---|---|
| `Dataset`, `Version` | 資料集與 KG snapshot provenance | `id`, `name`, `dataset_id`, `kg_version` |
| `Concept`, `Domain` | domain、概念與 category value 的語意範圍 | `name`, `concept_kind` |
| `LogicalEntity` | 與實體 table 解耦的實體 | `name`, `database` |
| `LogicalAttribute` | 語意屬性 | `name`, `description`, `database`, `table` |
| `Knowledge`, `Metric` | KB 計算知識 | `raw_definition`, `description`, `source_line` |
| `Knowledge`, `Rule` | domain rule；另加規則形狀 label | 同上 |
| `ThresholdRule`, `ClassificationRule`, `TemporalRule`, `EligibilityRule`, `OpaqueRule` | 可重現的粗粒度 rule classification | label 本身與 raw definition |
| `PhysicalSchemaItem`, `PhysicalTable`, `PhysicalColumn` | DDL schema | `database`, `table`, `name`, `data_type`, `nullable`, `primary_key` |
| `Expression`, `Formula`, `OpaqueExpression` | 公式或規則表示 | `raw_expression`, `parse_status`, `language` |
| `Literal`, `CategoryValue` | column 文件宣告的 category value | `value`, `name` |
| `Datatype` | DDL 宣告型別 | `name` |
| `Unit` | 由欄位 full name 明確抽出的單位 | `name` |
| `Grain` | 計算粒度；無證據時明確 unresolved | `name`, `status` |
| `AggregationPolicy` | 聚合政策；無證據時明確 unresolved | `name`, `status` |
| `TimeWindow` | 需要 evaluation time 的規則 | `raw_definition`, `evaluation_time_required` |
| `JoinCondition` | 由 DDL FK 形成的單一步驟 join predicate | `operator`, `expression`, `evidence` |
| `Assertion` | 對 schema/knowledge 的來源化宣告 | `assertion_kind`, `raw_text`, `confidence`, `status` |
| `ProvenanceSource` | 輸入檔案 | `path`, `sha256`, `source_kind` |

`Formula` 目前不是「已驗證 AST」：生成器只確認文字具有公式訊號，仍保存
`parse_status="raw"`。這可避免把 LaTeX/自然語言錯誤編譯成假的 executable truth。

## 關係模型

| 關係 | 起點 → 終點 | 意義 |
|---|---|---|
| `HAS_VERSION` | Dataset → Version | snapshot provenance |
| `PART_OF_VERSION` | ProvenanceSource → Version | 該版本使用的來源 |
| `VALID_IN_DOMAIN` | semantic node → Domain | 防止跨 database 同名概念誤合併 |
| `HAS_ATTRIBUTE` | LogicalEntity → LogicalAttribute | logical data model |
| `IMPLEMENTS` | Physical item → Logical entity/attribute | physical grounding |
| `HAS_DATATYPE` | PhysicalColumn → Datatype | DDL type assertion |
| `ALLOWS_VALUE` | LogicalAttribute → CategoryValue | 文件中的可能值；非 DB constraint |
| `MEASURED_IN` | LogicalAttribute → Unit | 明示單位 |
| `USES_ATTRIBUTE` | Metric/Rule → LogicalAttribute | definition 中唯一 exact identifier match |
| `DEPENDS_ON` | Knowledge → Knowledge | `children_knowledge`；不是 subclass |
| `DEFINED_BY` | Metric/Rule → Expression | 原始公式/規則表示 |
| `EVALUATED_AT_GRAIN` | Metric/Rule → Grain | evaluation grain |
| `HAS_AGGREGATION_POLICY` | Metric/Rule → AggregationPolicy | aggregation semantics |
| `HAS_TIME_WINDOW` | rule/metric → TimeWindow | query-time temporal semantics |
| `JOINABLE_VIA` | PhysicalColumn → JoinCondition | join 左側 |
| `JOINS_TO` | JoinCondition → PhysicalColumn | join 右側 |
| `ASSERTS_ABOUT` | Assertion → subject | assertion subject |
| `SUPPORTED_BY` | Assertion → ProvenanceSource | assertion provenance |

`JOINABLE_VIA` 不可做 transitive inference。跨多 table 的 join path 應在查詢時計算，不能
永久掛在 metric 上。`ALLOWS_VALUE` 只表示文件 evidence，不能當成完整 enum constraint。

## 常用查詢

### 1. 由使用者詞彙找 metric/rule 與 physical columns

```cypher
MATCH (k:Knowledge)
WHERE toLower(k.name) CONTAINS toLower($term)
OPTIONAL MATCH (k)-[u:USES_ATTRIBUTE]->(a:LogicalAttribute)
OPTIONAL MATCH (p:PhysicalColumn)-[:IMPLEMENTS]->(a)
RETURN k.id, labels(k), k.name, k.raw_definition,
       collect(DISTINCT {column: p.database + '.' + p.table + '.' + p.name,
                         confidence: u.confidence}) AS grounding;
```

參數 `$term = "Signal-to-Noise"` 可取得 SNQI 及其唯一 exact-match 欄位 grounding。

### 2. 展開 metric/rule dependency graph

```cypher
MATCH path=(k:Knowledge {id: $knowledge_id})-[:DEPENDS_ON*0..5]->(dependency:Knowledge)
RETURN path;
```

例如 `$knowledge_id = "knowledge:alien:50"`。注意方向忠實反映原始
`children_knowledge`，不表示 ontology hierarchy。

### 3. 尋找兩張 table 間 FK join path

因 join condition 被 reify，路徑交替經過 column 與 condition：

```cypher
MATCH (from:PhysicalTable {id: $from_id}), (to:PhysicalTable {id: $to_id})
MATCH p = shortestPath((from)-[:JOINABLE_VIA|JOINS_TO*..12]-(to))
RETURN [n IN nodes(p) | coalesce(n.expression, n.database+'.'+n.table+'.'+n.name)] AS path;
```

例如 `$from_id='physical:alien:table:signals'`、
`$to_id='physical:alien:table:observatories'`，路徑可經 `telescopes`。每個 FK 同時保留
table-level path edge 與 column-level evidence；正式 planner 應限制 domain/database，並將每個
`JoinCondition.expression` 編譯成 SQL predicate。

### 4. 取得可驗證 evidence pack

```cypher
MATCH (k:Knowledge {id: $knowledge_id})-[:DEFINED_BY]->(e:Expression)
MATCH (x:Assertion)-[:ASSERTS_ABOUT]->(k)
MATCH (x)-[:SUPPORTED_BY]->(s:ProvenanceSource)
OPTIONAL MATCH (k)-[:EVALUATED_AT_GRAIN]->(g:Grain)
OPTIONAL MATCH (k)-[:HAS_AGGREGATION_POLICY]->(ap:AggregationPolicy)
OPTIONAL MATCH (k)-[:HAS_TIME_WINDOW]->(tw:TimeWindow)
RETURN k.name, e.raw_expression, e.parse_status,
       g.name AS grain, ap.name AS aggregation,
       tw.raw_definition AS time_window,
       x.confidence, s.path, s.sha256, x.source_line;
```

### 5. 找待補規格的 metric

```cypher
MATCH (m:Metric)-[:EVALUATED_AT_GRAIN]->(g:Grain {status:'unresolved'})
MATCH (m)-[:HAS_AGGREGATION_POLICY]->(a:AggregationPolicy {status:'unresolved'})
RETURN m.database, m.id, m.name ORDER BY m.database, m.id;
```

## Mapping 範例

### Alien SNQI

```text
(Metric knowledge:alien:0)
  -[:DEFINED_BY]-> (Formula expression:alien:0 {parse_status:'raw'})
  -[:USES_ATTRIBUTE]-> (LogicalAttribute SnrRatio)
  -[:USES_ATTRIBUTE]-> (LogicalAttribute NoiseFloorDbm)

(PhysicalColumn signals.snrRatio)-[:IMPLEMENTS]->(LogicalAttribute SnrRatio)
(Assertion assertion:kb:alien:0)-[:ASSERTS_ABOUT]->(Metric)
(Assertion)-[:SUPPORTED_BY]->(ProvenanceSource alien/alien_kb.jsonl)
```

圖可定位公式、欄位與來源，但 `parse_status='raw'` 會阻止 compiler 把未驗證文字直接當 AST。

### TOLS 與分類規則

```text
(Metric knowledge:alien:3)-[:DEFINED_BY]->(Formula expression:alien:3)
(Rule knowledge:alien:52:ClassificationRule)-[:DEPENDS_ON]->(Metric knowledge:alien:3)
```

Rule label 是 deterministic heuristic，完整 ordered threshold clauses 尚未安全解析，仍以
`raw_definition` 保存。測評可比較「只用 raw」與後續人工/解析器提升的結構化版本。

### FK join

```text
(PhysicalColumn signals.telescRef)
  -[:JOINABLE_VIA]->
(JoinCondition {expression:'signals.telescRef = telescopes.telescRegistry'})
  -[:JOINS_TO]->
(PhysicalColumn telescopes.telescRegistry)
```

JoinCondition 有獨立 identity 與 DDL evidence，未把 query-specific 多跳 path 固化進 KG。

## 邊界與衝突政策

- ID 帶 database scope；同名 metric 不自動 `sameAs`。
- Column documentation 與 KB 具有 subject-level Assertion；DDL 來源以
  `ProvenanceSource -> Version` 保存 snapshot-level provenance，並直接形成 physical schema
  節點與 datatype/join 結構。三種來源不互相覆寫。
- schema TXT 的 sample rows 不匯入為 facts 或 enum constraint。
- JSONB nested documentation 目前 lossless 保存在 attribute description；沒有 DDL/runtime
  證據時不虛構 path datatype。
- KB 中無明示 grain、aggregation、NULL、zero-denominator、boundary policy 時一律 unresolved。
- datatype 是 DDL claim；documentation 中矛盾型別不覆寫它。
- 所有來源都有 SHA-256，可確認兩輪測評是否使用相同 snapshot。

因此 V2 的核心不是宣稱所有 KB 已可執行，而是讓「原始宣告、結構化推論、未知資訊與
物理 grounding」可以分開查詢和驗證。
