# Semantic Layer Design

## Graph 模型

```text
(:Database)-[:HAS_TABLE]->(:Table)-[:HAS_COLUMN]->(:Column)
                         |
                         +--[:HAS_FOREIGN_KEY]->(:ForeignKey)
                                                   |--[:REFERENCES_TABLE]->(:Table)
                                                   |--[:FROM_COLUMN]->(:Column)
                                                   `--[:TO_COLUMN]->(:Column)

(:Table)-[:JOINS_TO]->(:Table)
```

### Node 屬性

Neo4j property 不支援可儲存的 `null` 值；來源沒有 description 時，對應的
scalar property 不建立，API 讀取時回傳 `null`。`Table.description` 是例外：
因為目前來源沒有 table description，所有 Table node 都直接儲存空字串 `""`。

| Node label | Property | 型別 | 說明 |
| --- | --- | --- | --- |
| `Database` | `database_name` | string | Graph 內的 database 名稱，例如 `alien`。 |
| `Database` | `entity_key` | string | Database 的穩定識別值，格式為 `{database_name}`。 |
| `Table` | `database_name` | string | 所屬 database。 |
| `Table` | `table_name` | string | DDL 中的 table 名稱；目前以小寫 canonical name 儲存。 |
| `Table` | `description` | string | Table description；來源沒有 table description 時為空字串。 |
| `Table` | `entity_key` | string | Table 的穩定識別值，格式為 `{database_name}:{table_name}`。 |
| `Column` | `database_name` | string | 所屬 database。 |
| `Column` | `table_name` | string | 所屬 table。 |
| `Column` | `column_name` | string | DDL 中的 column 名稱；目前以小寫 canonical name 儲存。 |
| `Column` | `ordinal` | integer | Column 在 DDL table 定義中的順序，從 1 開始；API 依此排序。 |
| `Column` | `data_type` | string | 從 DDL 解析出的 PostgreSQL type，例如 `character varying`。 |
| `Column` | `nullable` | boolean | DDL 是否允許 NULL；primary key 視為不可為 NULL。 |
| `Column` | `default_expression` | string | DDL 的 default expression；沒有 default 時不建立此 property。 |
| `Column` | `is_primary_key` | boolean | 是否為 table primary key column。 |
| `Column` | `entity_key` | string | Column 的穩定識別值，格式為 `{database_name}:{table_name}:{column_name}`。 |
| `Column` | `raw_text` | string | 來源 column-meaning 的原始文字；structured meaning 會以 JSON string 保存。 |
| `Column` | `description` | string | Column description；若來源有 `Explanation:`，取其內容；否則取來源主要說明文字。 |
| `Column` | `declared_type` | string | 從 column-meaning 解析出的文件型別，例如 `VARCHAR(40)` 或 `enum`。 |
| `Column` | `examples` | list[string] | 從 column-meaning 的 `Example`/`e.g.` 解析出的範例值；沒有時為 `[]`。 |
| `ForeignKey` | `fk_key` | string | Foreign key 的 canonical key，包含 database、來源 table/columns 與目標 table/columns。 |
| `ForeignKey` | `from_columns` | list[string] | 來源 table 的 FK columns；支援 composite FK。 |
| `ForeignKey` | `to_columns` | list[string] | 目標 table 的 referenced columns；支援 composite FK。 |
| `ForeignKey` | `cardinality` | string | 目前為 `many_to_one` 或 `one_to_one`，由 FK 與 primary key 關係推導。 |
| `ForeignKey` | `nullable` | boolean | FK 來源 columns 是否有任一 column 可為 NULL。 |
| `ForeignKey` | `entity_key` | string | ForeignKey 的穩定識別值，格式為 `{database_name}:foreign_key:{fk_key}`。 |

Graph 中沒有獨立的 `TableDescription`、`ColumnDescription` node，也沒有
`HAS_DESCRIPTION` relationship。Column description properties 全部直接放
在 Column node 上；`ForeignKey` 是 canonical join assertion。

### Relationship 使用方式

| Relationship | 起點 -> 終點 | Relationship properties | 用途 |
| --- | --- | --- | --- |
| `HAS_TABLE` | `Database -> Table` | 無 | 表示 table 屬於哪個 database。 |
| `HAS_COLUMN` | `Table -> Column` | 無 | 保留 table 的 column 結構與 DDL 順序。 |
| `HAS_FOREIGN_KEY` | `Table -> ForeignKey` | 無 | 表示來源 table 宣告了哪一個 foreign key。 |
| `REFERENCES_TABLE` | `ForeignKey -> Table` | 無 | 表示 FK references 的目標 table。 |
| `FROM_COLUMN` | `ForeignKey -> Column` | 無 | 指向 FK 來源 columns。 |
| `TO_COLUMN` | `ForeignKey -> Column` | 無 | 指向 FK 目標 columns。 |
| `JOINS_TO` | `Table -> Table` | `fk_key`, `from_columns`, `to_columns`, `join_condition`, `cardinality` | 由 declared FK 產生的 read projection；用於 direct joins 與 bounded shortest-path search。沒有獨立的 JoinPath node。 |

`JOINS_TO` 的方向固定為 FK 來源 table -> referenced table，但 API 查詢
direct joins 與 join path 時會同時處理 outgoing/incoming traversal。所有
`join_paths` 都只使用 `JOINS_TO`，因此只會返回由 declared foreign keys
形成的 shortest paths。

## `get_table_schema` 使用方式

### Request

```json
{
  "database_name": "alien",
  "from_table": "signals",
  "to_table": "observatories",
  "include": ["columns", "descriptions", "constraints", "direct_joins"],
  "max_hops": 5,
  "max_paths": 5
}
```

| 欄位 | 型別 | 必填 | 預設值/限制 | 使用方式 |
| --- | --- | --- | --- | --- |
| `database_name` | string | 是 | 不可為空 | 以 request value 為準，在 kg-v1 graph 中查找同名 Database；不使用 task 的 selected database 覆蓋。 |
| `from_table` | string | 是 | 不可為空 | 起點 table。大小寫不敏感，但必須是 exact table name，不做 fuzzy matching。 |
| `to_table` | string | 否 | 無 | 目標 table。提供後自動回傳 `join_paths`；不需要也不能放入 `include`。與 `from_table` 相同會產生 `SAME_TABLE_PATH`。 |
| `include` | array[string] | 否 | `columns`、`descriptions`、`constraints`、`direct_joins` 全部包含 | 可選值只有四個；重複值會去重。 |
| `max_hops` | integer | 否 | `5`，範圍 `1..10` | 限制 shortest path 的最大 hop 數。 |
| `max_paths` | integer | 否 | `5`，範圍 `1..20` | 限制回傳的 shortest path 數量。 |

`database_name`、`from_table` 的值是 tool request 的 authoritative input。
`to_table` 只控制是否執行 path 查詢；`include` 只控制 table response 中的
資料區段。

### `include` 選項的作用

| Include 值 | Response 內容 |
| --- | --- |
| `columns` | 回傳 column structural fields：`column_name`、`ordinal`、`data_type`、`nullable`、`default_expression`、`is_primary_key`。 |
| `descriptions` | 回傳 Table `description`，並在每個 column 加上 `raw_text`、`description`、`declared_type`、`examples`。此值會一併帶出 columns，讓 column description 有承載位置。 |
| `constraints` | 回傳 primary key、nullable columns 與 foreign key 詳細資料。 |
| `direct_joins` | 回傳該 table 的一-hop incoming/outgoing FK join edges。 |

### Response

```json
{
  "tables": [
    {
      "role": "from",
      "table_name": "signals",
      "description": "",
      "columns": [
        {
          "column_name": "telescref",
          "ordinal": 3,
          "data_type": "character",
          "nullable": false,
          "default_expression": null,
          "is_primary_key": false,
          "raw_text": "Full name: 'Telescope Reference'. Explanation: Foreign key to the telescope used for detection. Data type: CHAR(20). Example: 'TELESC_0001'.",
          "description": "Foreign key to the telescope used for detection.",
          "declared_type": "CHAR(20)",
          "examples": ["TELESC_0001"]
        }
      ],
      "constraints": {
        "primary_key": ["signalregistry"],
        "nullable_columns": ["timemark", "detectinstr"],
        "foreign_keys": [
          {
            "fk_key": "alien:signals:telescref->telescopes:telescregistry",
            "from_columns": ["telescref"],
            "to_columns": ["telescregistry"],
            "to_table": "telescopes",
            "cardinality": "many_to_one",
            "nullable": false,
            "entity_key": "alien:foreign_key:signals:telescref->telescopes:telescregistry"
          }
        ]
      },
      "direct_joins": [
        {
          "target_table": "telescopes",
          "from_table": "signals",
          "to_table": "telescopes",
          "from_columns": ["telescref"],
          "to_columns": ["telescregistry"],
          "join_condition": "signals.telescref = telescopes.telescregistry",
          "cardinality": "many_to_one",
          "direction": "outgoing",
          "fk_key": "alien:signals:telescref->telescopes:telescregistry"
        }
      ]
    },
    {
      "role": "to",
      "table_name": "observatories",
      "description": "",
      "columns": [
        {
          "column_name": "observstation",
          "ordinal": 1,
          "data_type": "character",
          "nullable": false,
          "default_expression": null,
          "is_primary_key": true,
          "raw_text": "Full name: 'Observatory Name'. Explanation: This field holds the name or unique identifier for the observatory station. Data type: CHAR(60). Example: 'OBS_STATION_ALPHA'.",
          "description": "This field holds the name or unique identifier for the observatory station.",
          "declared_type": "CHAR(60)",
          "examples": ["OBS_STATION_ALPHA"]
        }
      ],
      "constraints": {
        "primary_key": ["observstation"],
        "nullable_columns": ["weathprofile", "seeingprofile"],
        "foreign_keys": []
      },
      "direct_joins": [
        {
          "target_table": "telescopes",
          "from_table": "telescopes",
          "to_table": "observatories",
          "from_columns": ["observstation"],
          "to_columns": ["observstation"],
          "join_condition": "telescopes.observstation = observatories.observstation",
          "cardinality": "many_to_one",
          "direction": "incoming",
          "fk_key": "alien:telescopes:observstation->observatories:observstation"
        }
      ]
    }
  ],
  "join_paths": [
    {
      "tables": ["signals", "telescopes", "observatories"],
      "hops": [
        {
          "from_table": "signals",
          "to_table": "telescopes",
          "from_columns": ["telescref"],
          "to_columns": ["telescregistry"],
          "join_condition": "signals.telescref = telescopes.telescregistry",
          "cardinality": "many_to_one",
          "direction": "outgoing",
          "fk_key": "alien:signals:telescref->telescopes:telescregistry"
        },
        {
          "from_table": "telescopes",
          "to_table": "observatories",
          "from_columns": ["observstation"],
          "to_columns": ["observstation"],
          "join_condition": "telescopes.observstation = observatories.observstation",
          "cardinality": "many_to_one",
          "direction": "outgoing",
          "fk_key": "alien:telescopes:observstation->observatories:observstation"
        }
      ]
    }
  ],
  "warnings": []
}
```

response 的 top-level 欄位如下：

| 欄位 | 型別 | 出現條件 | 說明 |
| --- | --- | --- | --- |
| `tables` | array[object] | 永遠 | `from_table` 在前；有 `to_table` 時，目標 table 在後。 |
| `join_paths` | array[object] | 有提供 `to_table` 時 | 所有符合 `max_hops` 的 shortest declared-FK paths，最多 `max_paths` 條。 |
| `warnings` | array[object] | 永遠 | Domain-partial warning；沒有 warning 時為 `[]`。 |

#### `tables[]`

| 欄位 | 型別 | 出現條件 | 說明 |
| --- | --- | --- | --- |
| `role` | string | 永遠 | `from` 或 `to`。 |
| `table_name` | string | 永遠 | Graph 中 canonical table name。 |
| `description` | string | `descriptions` | Table description；目前來源缺少 table description，因此為 `""`。 |
| `columns` | array[object] | `columns` 或 `descriptions` | 依 `ordinal` ascending 排列。 |
| `constraints` | object | `constraints` | Primary key、nullable columns、foreign keys。 |
| `direct_joins` | array[object] | `direct_joins` | 該 table 的一-hop join edges。 |

#### `columns[]`

| 欄位 | 型別 | 出現條件 | 說明 |
| --- | --- | --- | --- |
| `column_name` | string | `columns` 或 `descriptions` | Column 名稱。 |
| `ordinal` | integer | `columns` 或 `descriptions` | DDL 順序，從 1 開始。 |
| `data_type` | string | `columns` 或 `descriptions` | DDL type。 |
| `nullable` | boolean | `columns` 或 `descriptions` | 是否允許 NULL。 |
| `default_expression` | string/null | `columns` 或 `descriptions` | DDL default；沒有時為 `null`。 |
| `is_primary_key` | boolean | `columns` 或 `descriptions` | 是否為 PK column。 |
| `raw_text` | string/null | `descriptions` | 原始 column meaning；source 缺失時為 `null`。 |
| `description` | string/null | `descriptions` | 解析後的 column description；source 缺失時為 `null`。 |
| `declared_type` | string/null | `descriptions` | 文件中宣告的 type；來源沒有可解析型別時為 `null`。 |
| `examples` | array[string] | `descriptions` | 文件範例值；source 缺失或沒有範例時為 `[]`。 |

#### `constraints`

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `primary_key` | array[string] | 該 table 的 PK column names。 |
| `nullable_columns` | array[string] | 該 table 中 `nullable=true` 的 column names。 |
| `foreign_keys` | array[object] | 該 table 作為 FK source 的所有 declared foreign keys。 |

`constraints.foreign_keys[]` 的欄位：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `fk_key` | string | Canonical FK key。 |
| `from_columns` | array[string] | 來源 columns。 |
| `to_columns` | array[string] | 目標 columns。 |
| `to_table` | string | `REFERENCES_TABLE` 指向的目標 table。 |
| `cardinality` | string | `many_to_one` 或 `one_to_one`。 |
| `nullable` | boolean | 來源 FK columns 是否可為 NULL。 |
| `entity_key` | string | ForeignKey node 的 entity key。 |

#### `direct_joins[]`

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `target_table` | string | 從目前查詢 table 看到的另一端 table。 |
| `from_table` | string | Canonical FK source table。 |
| `to_table` | string | Canonical FK target table。 |
| `from_columns` | array[string] | Canonical FK source columns。 |
| `to_columns` | array[string] | Canonical FK target columns。 |
| `join_condition` | string | 例如 `signals.telescref = telescopes.telescregistry`。 |
| `cardinality` | string | `many_to_one` 或 `one_to_one`。 |
| `direction` | string | 相對於目前 table 的 `outgoing` 或 `incoming`。 |
| `fk_key` | string | 對應的 ForeignKey key。 |

#### `join_paths[]` 與 `hops[]`

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `join_paths[].tables` | array[string] | Path 上由起點到終點排列的 table names。 |
| `join_paths[].hops` | array[object] | 每一段 table-to-table FK join。 |
| `hops[].from_table` | string | 此 hop 的 traversal 起點。 |
| `hops[].to_table` | string | 此 hop 的 traversal 終點。 |
| `hops[].from_columns` | array[string] | 依 traversal 方向排列的來源 columns。 |
| `hops[].to_columns` | array[string] | 依 traversal 方向排列的目標 columns。 |
| `hops[].join_condition` | string | Canonical FK equality condition。 |
| `hops[].cardinality` | string | Canonical FK cardinality。 |
| `hops[].direction` | string | 相對於 canonical `JOINS_TO` 的 `outgoing` 或 `incoming`。 |
| `hops[].fk_key` | string | 此 hop 使用的 declared FK。 |


## Warning 與使用情境

Warning 不代表工具失敗；回應仍會成功回傳，agent 應根據 warning 調整後續
查詢或 SQL。`warnings` 永遠是 array。

| Warning code | scope | 觸發條件 | Response 行為 | 使用建議 |
| --- | --- | --- | --- | --- |
| `DESCRIPTION_MISSING` | `column` | DDL column 找不到對應的 column-meaning entry。 | 該 column 的 `raw_text`、`description`、`declared_type` 為 `null`，`examples` 為 `[]`。warning 會帶 `table`、`column`、`message`。 | 只依賴 DDL 的 column name/type/constraint；不要把缺少的 description 當成 schema 不存在。 |
| `NO_JOIN_PATH` | `path` | 提供 `to_table`，但在 `max_hops` 內找不到 declared-FK shortest path。 | `join_paths` 為 `[]`；warning 會帶 `from_table`、`to_table`、`message`。 | 先確認 database/table name，再提高 `max_hops`；若仍無 path，改用沒有 FK 宣告的 domain knowledge 或人工確認 join。 |

Table description 缺失不會產生 warning，因為這是已知來源條件，Table node
會固定儲存 `description: ""`。

## Tool errors（工具錯誤）

下列狀況是 hard failure，不會放在 `warnings` 中，而是以 tool error 回傳：

| Error code | 觸發條件 |
| --- | --- |
| `INVALID_REQUEST` | 必填欄位缺失/為空、`include` 含未知值，或 `max_hops`/`max_paths` 超出範圍。 |
| `DATABASE_NOT_FOUND` | `database_name` 不存在於 kg-v1 graph。 |
| `TABLE_NOT_FOUND` | `from_table` 或 `to_table` 不是該 database 中的 exact table。 |
| `SAME_TABLE_PATH` | 提供的 `to_table` 與 `from_table` 相同。 |
| `NEO4J_UNAVAILABLE` | Neo4j connection、authentication 或 database session 無法建立。 |
| `GRAPH_QUERY_FAILED` | Neo4j query 執行失敗或 graph 結構不符合 kg-v1 contract。 |
