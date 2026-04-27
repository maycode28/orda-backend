package com.orda.backend.domain.trail.entity;

import jakarta.persistence.*;
import lombok.AccessLevel;
import lombok.Getter;
import lombok.NoArgsConstructor;

@Entity
@Table(name = "trail_edges")
@Getter
@NoArgsConstructor(access = AccessLevel.PROTECTED)
public class TrailEdge {

    @Id
    @Column(name = "edge_id", nullable = false)
    private String edgeId;

    @Column(name = "source_gpx")
    private String sourceGpx;

    @Column(name = "start_node_id", nullable = false)
    private String startNodeId;

    @Column(name = "end_node_id", nullable = false)
    private String endNodeId;

    @Column(name = "distance_m")
    private Double distanceM;

    @Column(name = "elevation_start_m")
    private Double elevationStartM;

    @Column(name = "elevation_end_m")
    private Double elevationEndM;

    @Column(name = "elevation_diff_m")
    private Double elevationDiffM;

    @Column(name = "slope_percent")
    private Double slopePercent;

    @Column(name = "difficulty_score")
    private Double difficultyScore;

    @Column(name = "difficulty")
    private String difficulty;

    @Column(name = "nearest_summit_id")
    private String nearestSummitId;

    @Column(name = "qa_status")
    private String qaStatus;

    @Column(name = "surface")
    private String surface;

    public void updateDifficulty(String difficulty) {
        this.difficulty = difficulty;
    }
}