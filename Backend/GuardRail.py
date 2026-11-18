#Imported Libraries 
import sqlglot
from sqlglot import exp
import networkx as nx 
from enum import Enum
from pydantic import BaseModel
from typing import List, Dict, Any, Optional, Tuple 

class GuardrailDecision(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"      # Safe to delete 
    WARN_CONFIRM = "WARN_CONFIRM"      # Moderate impact; require explicit user confirmation
    BLOCKED = "BLOCKED"                # High/Critical impact; block deletion by default

class DeletionImpactAnalyzer(BaseModel):
    target_node: str
    decision: GuardrailDecision
    risk_score: float #Float might be changed in the future
    direct_dependents: List[str] # 1st Check for downstream nodes
    cascading_dependents: List[str] # 2nd Check for downstream nodes
    broken_edges: List[str] #Edges IDs severed in deletion
    message: str


def detect_destructive_targets(sql: str, dialect: str = "postgres") -> List[str]:
    """
    Step 1: AST Inspection
    Parses SQL to detect destructive operations (DROP, TRUNCATE, DELETE)
    and extracts the target table names slated for removal.
    """
    targets = []
    expressions = sqlglot.parse(sql, read=dialect)

    for expression in expressions:
        if not expression:
            continue

        # Detect DROP TABLE / VIEW
        if isinstance(expression, exp.Drop):
            if isinstance(expression.this, exp.Table):
                targets.append(expression.this.sql(dialect=dialect))

        # Detect TRUNCATE or DELETE
        elif isinstance(expression, (exp.TruncateTable, exp.Delete)):
            target = expression.this
            if isinstance(target, exp.Table):
                targets.append(target.sql(dialect=dialect))

    return list(set(targets))


def simulate_node_deletion(
    graph: nx.DiGraph, target_node: str
) -> Tuple[nx.DiGraph, List[str], List[str], List[str]]:
    """
    Step 2: Sandbox Simulation
    Clones the lineage DiGraph and calculates the exact blast radius
    without mutating the live production graph.
    """
    if target_node not in graph:
        raise ValueError(f"Target asset '{target_node}' does not exist in the lineage graph.")

    # Clone graph for safe sandbox simulation
    sandbox_graph = graph.copy()

    # Hop 1: Direct downstream dependents
    direct_dependents = list(sandbox_graph.successors(target_node))

    # All downstream reachable nodes (Hop 1 + Hop 2+)
    all_descendants = nx.descendants(sandbox_graph, target_node)
    cascading_dependents = sorted(list(all_descendants - set(direct_dependents)))

    # Identify all edges that will break if target_node and its downstream chain fail
    impacted_subgraph_nodes = all_descendants | {target_node}
    broken_edges = [
        f"{u}->{v}"
        for u, v in sandbox_graph.edges()
        if u in impacted_subgraph_nodes
    ]

    # Remove target in sandbox to verify remaining graph structure
    sandbox_graph.remove_node(target_node)

    return sandbox_graph, sorted(direct_dependents), cascading_dependents, broken_edges


def calculate_risk_score(
    graph: nx.DiGraph,
    direct_dependents: List[str],
    cascading_dependents: List[str],
    direct_weight: float = 25.0,
    cascading_weight: float = 10.0,
    terminal_bonus: float = 20.0,
) -> float:
    """
    Step 3: Risk Scoring Engine
    Computes a normalized 0-100 risk score based on hop distance and whether
    terminal sink nodes (e.g., reporting tables/dashboards) are affected.
    """
    if not direct_dependents and not cascading_dependents:
        return 0.0

    score = (len(direct_dependents) * direct_weight) + (
        len(cascading_dependents) * cascading_weight
    )

    # Check if any affected downstream node is a terminal sink (out_degree == 0)
    all_affected = set(direct_dependents) | set(cascading_dependents)
    terminal_hits = sum(1 for node in all_affected if graph.out_degree(node) == 0)
    score += terminal_hits * terminal_bonus

    return min(round(score, 2), 100.0)


def evaluate_deletion_guardrail(
    graph: nx.DiGraph,
    target_node: str,
    warn_threshold: float = 25.0,
    block_threshold: float = 65.0,
) -> DeletionImpactAnalyzer:
    """
    Step 4: Policy Gate & Decision Output
    Runs the full what-if pipeline and returns a structured report for the UI/API.
    """
    _, direct, cascading, broken_edges = simulate_node_deletion(graph, target_node)
    risk_score = calculate_risk_score(graph, direct, cascading)

    if risk_score >= block_threshold:
        decision = GuardrailDecision.BLOCKED
        message = (
            f"Deletion blocked (Risk Score: {risk_score}/100). "
            f"Removing '{target_node}' breaks {len(direct)} direct and "
            f"{len(cascading)} cascading downstream assets."
        )
    elif risk_score >= warn_threshold:
        decision = GuardrailDecision.WARN_CONFIRM
        message = (
            f"Confirmation required (Risk Score: {risk_score}/100). "
            f"Removing '{target_node}' impacts {len(direct) + len(cascading)} downstream asset(s)."
        )
    else:
        decision = GuardrailDecision.AUTO_APPROVE
        message = f"Safe to delete '{target_node}'. Zero downstream dependencies detected."

    return DeletionImpactAnalyzer(
        target_node=target_node,
        decision=decision,
        risk_score=risk_score,
        direct_dependents=direct,
        cascading_dependents=cascading,
        broken_edges=broken_edges,
        message=message,
    )


if __name__ == "__main__":
    # Quick verification against a sample lineage graph
    from Graphing import build_lineage_graph

    setup_sql = """
    CREATE TABLE analytics.user_summary AS
    SELECT user_id, email FROM raw.users;

    CREATE TABLE reporting.exec_kpis AS
    SELECT COUNT(user_id) AS active_count FROM analytics.user_summary;
    """
    g = build_lineage_graph(setup_sql)

    # Simulate deleting an upstream source table
    report = evaluate_deletion_guardrail(g, target_node="raw.users")
    print(report.model_dump_json(indent=2))
