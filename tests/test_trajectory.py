"""Tests for trajectory-based early-trend detection."""

import unittest
from datetime import UTC, datetime, timedelta

from detect_trajectory import (
    TrajectoryAnalyzer,
    TrajectoryMetrics,
    analyze_candidate_trajectory,
    batch_analyze_trajectories,
    filter_early_trends,
)


class TrajectoryAnalyzerTests(unittest.TestCase):
    def test_calculate_velocity_with_growth(self):
        """Test velocity calculation for a growing trend."""
        analyzer = TrajectoryAnalyzer()
        now = datetime.now(UTC)
        
        # Growing trend: 10 -> 20 -> 40 mentions over 3 days
        mention_counts = [
            (now - timedelta(days=2), 10),
            (now - timedelta(days=1), 20),
            (now, 40),
        ]
        
        velocity = analyzer.calculate_velocity(mention_counts)
        
        # Should be positive and significant
        self.assertGreater(velocity, 0.1)
        self.assertLessEqual(velocity, 1.0)
    
    def test_calculate_velocity_flat(self):
        """Test velocity calculation for a flat trend."""
        analyzer = TrajectoryAnalyzer()
        now = datetime.now(UTC)
        
        # Flat trend: constant mentions
        mention_counts = [
            (now - timedelta(days=2), 10),
            (now - timedelta(days=1), 10),
            (now, 10),
        ]
        
        velocity = analyzer.calculate_velocity(mention_counts)
        
        # Should be near zero
        self.assertAlmostEqual(velocity, 0.0, places=2)
    
    def test_calculate_velocity_empty(self):
        """Test velocity with empty data."""
        analyzer = TrajectoryAnalyzer()
        
        velocity = analyzer.calculate_velocity([])
        
        self.assertEqual(velocity, 0.0)
    
    def test_calculate_acceleration_positive(self):
        """Test acceleration calculation for accelerating growth."""
        analyzer = TrajectoryAnalyzer()
        now = datetime.now(UTC)
        
        # Accelerating: 10 -> 15 (1.5x) -> 30 (2x)
        mention_counts = [
            (now - timedelta(days=3), 10),
            (now - timedelta(days=2), 10),
            (now - timedelta(days=1), 15),
            (now - timedelta(days=1), 15),
            (now, 30),
            (now, 30),
        ]
        
        acceleration = analyzer.calculate_acceleration(mention_counts)
        
        # Should be positive (growth speeding up)
        self.assertGreater(acceleration, 0.0)
        self.assertLessEqual(acceleration, 1.0)
    
    def test_calculate_acceleration_negative(self):
        """Test acceleration calculation for decelerating growth."""
        analyzer = TrajectoryAnalyzer()
        now = datetime.now(UTC)
        
        # Decelerating: 10 -> 20 (2x) -> 25 (1.25x)
        mention_counts = [
            (now - timedelta(days=3), 10),
            (now - timedelta(days=2), 10),
            (now - timedelta(days=1), 20),
            (now - timedelta(days=1), 20),
            (now, 25),
            (now, 25),
        ]
        
        acceleration = analyzer.calculate_acceleration(mention_counts)
        
        # Should be negative or near zero (growth slowing)
        self.assertLessEqual(acceleration, 0.2)
    
    def test_classify_direction_rising(self):
        """Test direction classification for rising trend."""
        analyzer = TrajectoryAnalyzer()
        
        direction = analyzer.classify_direction(velocity=0.6, acceleration=0.1)
        
        self.assertEqual(direction, "rising")
    
    def test_classify_direction_falling(self):
        """Test direction classification for falling trend."""
        analyzer = TrajectoryAnalyzer()
        
        # High velocity but negative acceleration
        direction = analyzer.classify_direction(velocity=0.6, acceleration=-0.3)
        
        self.assertEqual(direction, "falling")
    
    def test_classify_direction_flat(self):
        """Test direction classification for flat trend."""
        analyzer = TrajectoryAnalyzer()
        
        direction = analyzer.classify_direction(velocity=0.05, acceleration=0.0)
        
        self.assertEqual(direction, "flat")
    
    def test_compute_early_trend_score_high_novelty_rising(self):
        """Test early-trend score for ideal candidate."""
        analyzer = TrajectoryAnalyzer()
        
        score = analyzer.compute_early_trend_score(
            novelty=0.85,
            velocity=0.6,
            acceleration=0.2,
        )
        
        # High novelty + rising velocity + positive acceleration = high score
        self.assertGreater(score, 0.6)
        self.assertLessEqual(score, 1.0)
    
    def test_compute_early_trend_score_low_novelty(self):
        """Test early-trend score for already-popular topic."""
        analyzer = TrajectoryAnalyzer()
        
        low_novelty_score = analyzer.compute_early_trend_score(
            novelty=0.2,  # Low novelty (already mainstream)
            velocity=0.8,  # But high velocity
            acceleration=0.1,
        )
        
        high_novelty_score = analyzer.compute_early_trend_score(
            novelty=0.8,  # High novelty
            velocity=0.8,
            acceleration=0.1,
        )
        
        # Low novelty should result in lower score than high novelty
        self.assertLess(low_novelty_score, high_novelty_score)
        # But with high velocity, it can still be moderate
        self.assertGreater(low_novelty_score, 0.3)
    
    def test_compute_early_trend_score_decelerating(self):
        """Test early-trend score for peaking trend."""
        analyzer = TrajectoryAnalyzer()
        
        score = analyzer.compute_early_trend_score(
            novelty=0.7,
            velocity=0.6,
            acceleration=-0.3,  # Decelerating
        )
        
        # Negative acceleration should reduce score
        self.assertLess(score, 0.7)
    
    def test_is_early_trend_true(self):
        """Test early-trend detection for qualifying candidate."""
        analyzer = TrajectoryAnalyzer()
        
        metrics = TrajectoryMetrics(
            velocity=0.6,
            acceleration=0.2,
            direction="rising",
            early_trend_score=0.75,
            novelty_score=0.7,
            reasoning="Test",
        )
        
        self.assertTrue(analyzer.is_early_trend(metrics))
    
    def test_is_early_trend_false_low_score(self):
        """Test early-trend detection for non-qualifying candidate."""
        analyzer = TrajectoryAnalyzer()
        
        metrics = TrajectoryMetrics(
            velocity=0.2,
            acceleration=-0.1,
            direction="falling",
            early_trend_score=0.3,
            novelty_score=0.2,
            reasoning="Test",
        )
        
        self.assertFalse(analyzer.is_early_trend(metrics))
    
    def test_analyze_trend_full(self):
        """Test full trend analysis."""
        analyzer = TrajectoryAnalyzer()
        now = datetime.now(UTC)
        
        mention_counts = [
            (now - timedelta(days=2), 10),
            (now - timedelta(days=1), 25),
            (now, 50),
        ]
        
        metrics = analyzer.analyze_trend(
            trend_text="inverted fullbacks in build-up",
            trend_embedding=None,  # Will use default novelty
            mention_counts=mention_counts,
            source_count=3,
            conn=None,
        )
        
        self.assertIsInstance(metrics, TrajectoryMetrics)
        self.assertGreater(metrics.velocity, 0)
        self.assertIn(metrics.direction, ["rising", "falling", "flat"])
        self.assertGreaterEqual(metrics.early_trend_score, 0)
        self.assertLessEqual(metrics.early_trend_score, 1)
        self.assertIsNotNone(metrics.reasoning)


class BatchAnalysisTests(unittest.TestCase):
    def test_batch_analyze_trajectories_empty(self):
        """Test batch analysis with empty candidates."""
        candidates = []
        
        result = batch_analyze_trajectories(None, candidates)
        
        self.assertEqual(result, [])
    
    def test_batch_analyze_trajectories_defaults(self):
        """Test batch analysis adds default trajectory fields."""
        candidates = [
            {
                "trend": "test trend",
                "novelty_score": 0.6,
                "source_diversity": 2,
            }
        ]
        
        result = batch_analyze_trajectories(None, candidates)
        
        self.assertEqual(len(result), 1)
        self.assertIn("velocity_score", result[0])
        self.assertIn("acceleration_score", result[0])
        self.assertIn("trajectory_direction", result[0])
        self.assertIn("early_trend_score", result[0])
    
    def test_analyze_candidate_trajectory_no_history(self):
        """Test candidate analysis without mention history."""
        candidate = {
            "trend": "test trend",
            "novelty_score": 0.6,
        }
        
        result = analyze_candidate_trajectory(None, candidate, mention_history=None)
        
        # Should get default values
        self.assertEqual(result["velocity_score"], 0.0)
        self.assertEqual(result["acceleration_score"], 0.0)
        self.assertEqual(result["trajectory_direction"], "flat")


class FilterEarlyTrendsTests(unittest.TestCase):
    def test_filter_early_trends_basic(self):
        """Test filtering to early-trends only."""
        candidates = [
            {"trend": "rising 1", "early_trend_score": 0.7, "trajectory_direction": "rising"},
            {"trend": "rising 2", "early_trend_score": 0.6, "trajectory_direction": "rising"},
            {"trend": "flat", "early_trend_score": 0.3, "trajectory_direction": "flat"},
            {"trend": "falling", "early_trend_score": 0.4, "trajectory_direction": "falling"},
        ]
        
        filtered = filter_early_trends(candidates, min_early_trend_score=0.5, require_rising=True)
        
        # Should only include rising trends with score >= 0.5
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0]["trend"], "rising 1")  # Highest score first
        self.assertEqual(filtered[1]["trend"], "rising 2")
    
    def test_filter_early_trends_no_rising_requirement(self):
        """Test filtering without requiring rising direction."""
        candidates = [
            {"trend": "high flat", "early_trend_score": 0.8, "trajectory_direction": "flat"},
            {"trend": "low rising", "early_trend_score": 0.4, "trajectory_direction": "rising"},
        ]
        
        filtered = filter_early_trends(candidates, min_early_trend_score=0.5, require_rising=False)
        
        # Should include high flat even though not rising
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["trend"], "high flat")
    
    def test_filter_early_trends_sorting(self):
        """Test that filtered results are sorted by early_trend_score."""
        candidates = [
            {"trend": "medium", "early_trend_score": 0.6, "trajectory_direction": "rising"},
            {"trend": "high", "early_trend_score": 0.9, "trajectory_direction": "rising"},
            {"trend": "low", "early_trend_score": 0.5, "trajectory_direction": "rising"},
        ]
        
        filtered = filter_early_trends(candidates, min_early_trend_score=0.5)
        
        scores = [c["early_trend_score"] for c in filtered]
        self.assertEqual(scores, [0.9, 0.6, 0.5])  # Descending order


if __name__ == "__main__":
    unittest.main()
