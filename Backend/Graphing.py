# Imported Libraries 
import os
import sqlglot
from sqlglot import exp
import networkx as nx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title = "Data Lineage Graphing Service")
class SQLPayload(BaseModel):
    sql: str
    dialect: Optional[str] = "postgres"

def build_lineage_graph(sql: str, dialect: str = "postgres") -> nx.DiGraph:
    """Parses SQL statements and constructs a directed lineage graph (Source -> Target)."""
    graph = nx.DiGraph()
    
    try:
        expressions = sqlglot.parse(sql, read=dialect)
    except Exception as e:
        raise ValueError(f"SQL parsing error: {str(e)}")

    for expression in expressions:
        if not expression:
            continue

        target_tables = set()
        
        # Identify target tables (CREATE TABLE, INSERT INTO, MERGE, UPDATE)
        if isinstance(expression, (exp.Create, exp.Insert, exp.Update, exp.Merge)):
            target = expression.this
            if isinstance(target, exp.Schema):
                target = target.this
            if isinstance(target, exp.Table):
                target_name = target.sql(dialect=dialect)
                target_tables.add(target_name)
                graph.add_node(target_name, type="table", role="target")

        # Extract all referenced tables
        all_tables = set()
        for table in expression.find_all(exp.Table):
            table_name = table.sql(dialect=dialect)
            all_tables.add(table_name)
            if table_name not in graph:
                graph.add_node(table_name, type="table", role="source")

        # Identify CTEs to avoid treating temporary CTE names as physical source tables
        ctes = {cte.alias_or_name for cte in expression.find_all(exp.CTE)}
        source_tables = all_tables - target_tables - ctes

        # Add directed edges from sources to targets
        for tgt in target_tables:
            for src in source_tables:
                graph.add_edge(src, tgt, relation="DERIVES_FROM")

    return graph

def serialize_graph(graph: nx.DiGraph) -> Dict[str, Any]:
    """Formats NetworkX DiGraph into React Flow / frontend-compatible nodes and edges."""
    nodes = [
        {
            "id": node,
            "data": {"label": node, **attrs},
            "type": attrs.get("role", "default")
        }
        for node, attrs in graph.nodes(data=True)
    ]
    
    edges = [
        {
            "id": f"{u}->{v}",
            "source": u,
            "target": v,
            "label": attrs.get("relation", "")
        }
        for u, v, attrs in graph.edges(data=True)
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "metrics": {
            "node_count": graph.number_of_nodes(),
            "edge_count": graph.number_of_edges(),
            "is_dag": nx.is_directed_acyclic_graph(graph)
        }
    }

@app.post("/api/lineage/parse")
async def parse_sql_lineage(payload: SQLPayload):
    try:
        graph = build_lineage_graph(payload.sql, payload.dialect)
        return serialize_graph(graph)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    # Quick local test run
    sample_sql = """
    CREATE TABLE analytics.user_summary AS
    WITH active_users AS (
        SELECT user_id, email FROM raw.users WHERE status = 'active'
    )
    SELECT a.user_id, a.email, COUNT(o.order_id) AS total_orders
    FROM active_users a
    JOIN raw.orders o ON a.user_id = o.user_id
    GROUP BY a.user_id, a.email;
    """
    test_graph = build_lineage_graph(sample_sql)
    print("Sample Lineage Graph:", serialize_graph(test_graph))
    uvicorn.run(app, host="0.0.0.0", port=8000)