"""Trajectory-based early-trend detection for football content.

Analyzes trend velocity and acceleration to identify trends BEFORE they peak.
Combines with semantic novelty to predict "about to be popular" vs "already popular".
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional

import numpy as np

from novelty_scoring import compute_novelty_score

log = logging.getLogger("research")


@dataclass
class TrajectoryMetrics:
    """Trajectory metrics for a trend candidate."""
    velocity: float  # Growth rate (mentions per day normalized)
    acceleration: float  # Change in velocity (second derivative)
    direction: str  # 'rising', 'falling', or 'flat'
    early_trend_score: float  # Combined score (0-1) for early-trend detection
    novelty_score: float  # Semantic novelty (0-1)
    reasoning: str  # Human-readable explanation


class TrajectoryAnalyzer:
    """Analyzes trend trajectories for early-trend detection.
    
    Focuses on spotting trends BEFORE they peak by combining:
    - Velocity: How fast is the trend growing
    - Acceleration: Is growth speeding up or slowing down
    - Novelty: How semantically distinct from historical patterns
    """
    
    def __init__(
        self,
        velocity_window_days: int = 3,
        history_window_days: int = 7,
        velocity_threshold: float = 0.5,
        acceleration_threshold: float = 0.1,
        early_trend_novelty_weight: float = 0.4,
        early_trend_velocity_weight: float = 0.35,
        early_trend_acceleration_weight: float = 0.25,
    ):
        self.velocity_window_days = velocity_window_days
        self.history_window_days = history_window_days
        self.velocity_threshold = velocity_threshold
        self.acceleration_threshold = acceleration_threshold
        self.early_trend_novelty_weight = early_trend_novelty_weight
        self.early_trend_velocity_weight = early_trend_velocity_weight
        self.early_trend_acceleration_weight = early_trend_acceleration_weight
    
    def calculate_velocity(
        self,
        mention_counts: list[tuple[datetime, int]],
    ) -> float:
        """Calculate trend velocity from mention counts over time.
        
        Velocity = normalized growth rate over the velocity window.
        Returns value where:
        - 0 = no growth
        - 1 = moderate growth
        - >1 = rapid growth
        """
        if not mention_counts or len(mention_counts) < 2:
            return 0.0
        
        # Sort by timestamp
        sorted_counts = sorted(mention_counts, key=lambda x: x[0])
        
        # Get recent window
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self.velocity_window_days)
        recent = [(ts, count) for ts, count in sorted_counts if ts >= cutoff]
        
        if len(recent) < 2:
            # Not enough recent data, use all available
            recent = sorted_counts[-min(len(sorted_counts), 5):]
        
        if len(recent) < 2:
            return 0.0
        
        # Calculate growth rate
        earliest_ts, earliest_count = recent[0]
        latest_ts, latest_count = recent[-1]
        
        days_diff = max(1, (latest_ts - earliest_ts).total_seconds() / 86400)
        
        if earliest_count == 0:
            # Avoid division by zero - use absolute growth
            velocity = min(latest_count / days_diff, 5.0)  # Cap at 5x
        else:
            growth_rate = (latest_count - earliest_count) / earliest_count
            velocity = growth_rate / days_diff
        
        # Normalize: 0-1 is normal, 1-3 is high, >3 is very high
        normalized_velocity = min(velocity, 5.0) / 5.0
        
        return round(normalized_velocity, 4)
    
    def calculate_acceleration(
        self,
        mention_counts: list[tuple[datetime, int]],
    ) -> float:
        """Calculate trend acceleration (change in velocity).
        
        Positive acceleration = growth is speeding up (early trend)
        Negative acceleration = growth is slowing down (peaking/declining)
        Near zero = steady state
        """
        if not mention_counts or len(mention_counts) < 3:
            return 0.0
        
        # Sort by timestamp
        sorted_counts = sorted(mention_counts, key=lambda x: x[0])
        
        # Split into two halves for velocity comparison
        mid = len(sorted_counts) // 2
        first_half = sorted_counts[:mid]
        second_half = sorted_counts[mid:]
        
        if len(first_half) < 1 or len(second_half) < 1:
            return 0.0
        
        # Calculate velocity for each half
        def half_velocity(half):
            if len(half) < 2:
                return 0.0
            ts_start, count_start = half[0]
            ts_end, count_end = half[-1]
            days = max(1, (ts_end - ts_start).total_seconds() / 86400)
            if count_start == 0:
                return min(count_end / days, 5.0)
            return ((count_end - count_start) / count_start) / days
        
        velocity_first = half_velocity(first_half)
        velocity_second = half_velocity(second_half)
        
        # Acceleration = change in velocity
        raw_acceleration = velocity_second - velocity_first
        
        # Normalize to -1 to 1 range
        normalized_acceleration = max(-1.0, min(1.0, raw_acceleration))
        
        return round(normalized_acceleration, 4)
    
    def classify_direction(self, velocity: float, acceleration: float) -> str:
        """Classify trajectory direction based on velocity and acceleration.
        
        Returns:
            'rising': Growing with positive momentum (best for early detection)
            'falling': Declining or slowing growth
            'flat': Stable or minimal activity
        """
        if velocity < 0.1:
            return "flat"
        
        if velocity > 0 and acceleration >= -0.1:
            # Growing with positive or neutral acceleration
            return "rising"
        
        if velocity > 0 and acceleration < -0.1:
            # Growing but decelerating
            return "falling"
        
        if velocity <= 0 or acceleration < -0.2:
            return "falling"
        
        return "flat"
    
    def compute_early_trend_score(
        self,
        novelty: float,
        velocity: float,
        acceleration: float,
    ) -> float:
        """Compute combined early-trend score.
        
        High scores indicate trends that are:
        - Semantically novel (not already mainstream)
        - Growing (positive velocity)
        - Accelerating (momentum building)
        
        Formula weights novelty heavily - we want novel trends that are
        starting to gain traction, not already-popular topics.
        """
        # Normalize inputs to 0-1
        novelty_norm = max(0.0, min(1.0, novelty))
        velocity_norm = max(0.0, min(1.0, velocity))
        # Acceleration: -1 to 1 -> 0 to 1 (shift and scale)
        acceleration_norm = max(0.0, min(1.0, (acceleration + 1) / 2))
        
        # Weighted combination
        score = (
            novelty_norm * self.early_trend_novelty_weight +
            velocity_norm * self.early_trend_velocity_weight +
            acceleration_norm * self.early_trend_acceleration_weight
        )
        
        # Bonus for positive acceleration with decent velocity (true early trend)
        if velocity > 0.3 and acceleration > 0:
            score = min(1.0, score * 1.15)
        
        # Penalty for negative acceleration (trend peaking/declining)
        if acceleration < -0.2:
            score = score * 0.7
        
        return round(score, 4)
    
    def analyze_trend(
        self,
        trend_text: str,
        trend_embedding: list[float],
        mention_counts: list[tuple[datetime, int]],
        source_count: int = 1,
        conn=None,
    ) -> TrajectoryMetrics:
        """Full trajectory analysis for a trend candidate.
        
        Args:
            trend_text: Description of the trend
            trend_embedding: Vector embedding of the trend
            mention_counts: List of (timestamp, mention_count) tuples
            source_count: Number of independent sources
            conn: Database connection for novelty scoring
        
        Returns:
            TrajectoryMetrics with all computed values
        """
        # Calculate velocity and acceleration
        velocity = self.calculate_velocity(mention_counts)
        acceleration = self.calculate_acceleration(mention_counts)
        direction = self.classify_direction(velocity, acceleration)
        
        # Get novelty score
        novelty = 0.5
        if conn and trend_embedding:
            try:
                novelty = compute_novelty_score(conn, trend_text, trend_embedding, source_count)
            except Exception as e:
                log.warning(f"Novelty scoring failed for '{trend_text[:50]}': {e}")
        
        # Compute early-trend score
        early_trend_score = self.compute_early_trend_score(novelty, velocity, acceleration)
        
        # Build reasoning string
        velocity_desc = "rapid" if velocity > 0.6 else "moderate" if velocity > 0.2 else "slow"
        accel_desc = "accelerating" if acceleration > 0.1 else "steady" if acceleration > -0.1 else "decelerating"
        novelty_desc = "high" if novelty > 0.7 else "medium" if novelty > 0.4 else "low"
        
        reasoning = (
            f"{velocity_desc.capitalize()} growth ({velocity:.1f}x) + "
            f"{accel_desc} + "
            f"{novelty_desc} novelty ({novelty:.2f})"
        )
        
        return TrajectoryMetrics(
            velocity=velocity,
            acceleration=acceleration,
            direction=direction,
            early_trend_score=early_trend_score,
            novelty_score=novelty,
            reasoning=reasoning,
        )
    
    def is_early_trend(self, metrics: TrajectoryMetrics) -> bool:
        """Determine if this is an early-trend worth flagging.
        
        Criteria for early-trend:
        - Early trend score >= 0.5
        - Direction is rising
        - Velocity above threshold
        - Novelty above minimum
        """
        return (
            metrics.early_trend_score >= 0.5
            and metrics.direction == "rising"
            and metrics.velocity >= self.velocity_threshold
            and metrics.novelty_score >= 0.3
        )


def analyze_candidate_trajectory(
    conn,
    candidate: dict,
    mention_history: Optional[list[tuple[datetime, int]]] = None,
    analyzer: Optional[TrajectoryAnalyzer] = None,
) -> dict:
    """Analyze trajectory for a single trend candidate.
    
    Updates the candidate dict with trajectory metrics.
    
    Args:
        conn: Database connection
        candidate: Trend candidate dict with 'trend', 'novelty_score', etc.
        mention_history: Optional pre-fetched mention counts over time
        analyzer: Optional TrajectoryAnalyzer instance
    
    Returns:
        Updated candidate dict with trajectory fields
    """
    if analyzer is None:
        analyzer = TrajectoryAnalyzer()
    
    trend_text = candidate.get("trend", "")
    trend_embedding = candidate.get("embedding") or candidate.get("vector")
    source_count = candidate.get("source_diversity", 1)
    
    # Fetch mention history from DB if not provided
    if mention_history is None and conn:
        mention_history = _fetch_mention_history(conn, trend_text)
    
    if not mention_history:
        # No history available - use defaults
        candidate["velocity_score"] = 0.0
        candidate["acceleration_score"] = 0.0
        candidate["trajectory_direction"] = "flat"
        candidate["early_trend_score"] = candidate.get("novelty_score", 0.5) * 0.5
        candidate["trajectory_reasoning"] = "Insufficient history for trajectory analysis"
        return candidate
    
    # Analyze trajectory
    metrics = analyzer.analyze_trend(
        trend_text=trend_text,
        trend_embedding=trend_embedding,
        mention_counts=mention_history,
        source_count=source_count,
        conn=conn,
    )
    
    # Update candidate with trajectory data
    candidate["velocity_score"] = metrics.velocity
    candidate["acceleration_score"] = metrics.acceleration
    candidate["trajectory_direction"] = metrics.direction
    candidate["early_trend_score"] = metrics.early_trend_score
    candidate["trajectory_reasoning"] = metrics.reasoning
    
    return candidate


def _fetch_mention_history(
    conn,
    trend_text: str,
    days: int = 14,
) -> list[tuple[datetime, int]]:
    """Fetch mention count history for a trend from the database.
    
    Looks for similar content in chunks and counts mentions per day.
    """
    try:
        with conn.cursor() as cur:
            # Search for content matching the trend keywords
            keywords = _extract_keywords(trend_text)
            if not keywords:
                return []
            
            # Build search query
            tsquery = " | ".join(keywords[:5])  # Use top 5 keywords
            
            cur.execute(
                """
                SELECT 
                    DATE(s.created_at) as day,
                    COUNT(*) as mentions
                FROM chunks c
                JOIN sources s ON c.source_id = s.id
                WHERE c.search_tsv @@ to_tsquery('simple', %s)
                    AND s.created_at > NOW() - INTERVAL '%s days'
                GROUP BY DATE(s.created_at)
                ORDER BY day
                """,
                (tsquery, days),
            )
            
            results = []
            for row in cur.fetchall():
                if row[0]:
                    dt = row[0] if isinstance(row[0], datetime) else datetime.combine(row[0], datetime.min.time())
                    dt = dt.replace(tzinfo=UTC)
                    results.append((dt, row[1]))
            
            return results
    except Exception as e:
        log.warning(f"Failed to fetch mention history for '{trend_text[:50]}': {e}")
        return []


def _extract_keywords(text: str) -> list[str]:
    """Extract tactical keywords from trend text for search."""
    # Common football tactical terms
    tactical_terms = {
        "back", "backs", "build-up", "counterpress", "cross", "crosses",
        "defender", "defenders", "full-back", "fullbacks", "goal-kick",
        "half-space", "inverts", "midfield", "midfielder", "overload",
        "press", "pressing", "set-piece", "striker", "wing-back",
        "winger", "zone", "possession", "transition", "counterattack",
        "offside", "offside-trap", "high-line", "low-block", "mid-block",
        "false-nine", "inverted-fullback", "inverted-winger",
    }
    
    words = text.lower().split()
    keywords = [w for w in words if w in tactical_terms or len(w) > 4]
    
    # Remove common stop words
    stop_words = {"the", "and", "for", "are", "but", "not", "you", "all", "can", "had", "her", "was", "one", "our", "out", "day", "get", "has", "him", "his", "how", "its", "may", "new", "now", "old", "see", "two", "way", "who", "boy", "did", "she", "use", "her", "now", "him", "than", "them", "well", "were"}
    keywords = [k for k in keywords if k not in stop_words]
    
    return keywords[:10]


def batch_analyze_trajectories(
    conn,
    candidates: list[dict],
    analyzer: Optional[TrajectoryAnalyzer] = None,
) -> list[dict]:
    """Analyze trajectories for multiple candidates.
    
    Args:
        conn: Database connection
        candidates: List of trend candidate dicts
        analyzer: Optional TrajectoryAnalyzer instance
    
    Returns:
        List of candidates updated with trajectory metrics
    """
    if analyzer is None:
        analyzer = TrajectoryAnalyzer()
    
    analyzed = []
    for candidate in candidates:
        try:
            updated = analyze_candidate_trajectory(conn, candidate, analyzer=analyzer)
            analyzed.append(updated)
        except Exception as e:
            log.warning(f"Trajectory analysis failed for candidate: {e}")
            # Keep original candidate with default values
            candidate["velocity_score"] = 0.0
            candidate["acceleration_score"] = 0.0
            candidate["trajectory_direction"] = "flat"
            candidate["early_trend_score"] = 0.0
            candidate["trajectory_reasoning"] = "Analysis failed"
            analyzed.append(candidate)
    
    return analyzed


def filter_early_trends(
    candidates: list[dict],
    min_early_trend_score: float = 0.5,
    require_rising: bool = True,
) -> list[dict]:
    """Filter candidates to only early-trends.
    
    Args:
        candidates: List of candidates with trajectory metrics
        min_early_trend_score: Minimum score to qualify
        require_rising: If True, only include rising trajectories
    
    Returns:
        Filtered list of early-trend candidates
    """
    filtered = []
    for c in candidates:
        score = c.get("early_trend_score", 0)
        direction = c.get("trajectory_direction", "flat")
        
        if score >= min_early_trend_score:
            if not require_rising or direction == "rising":
                filtered.append(c)
    
    # Sort by early_trend_score descending
    filtered.sort(key=lambda x: -x.get("early_trend_score", 0))
    
    return filtered
