package com.orda.backend.domain.recommendation.dto.response;

import com.fasterxml.jackson.annotation.JsonProperty;

public record RecommendationItemResponse(
        @JsonProperty("trail_id")
        String trailId,
        String name,
        @JsonProperty("distance_km")
        Double distanceKm,
        @JsonProperty("elevation_gain_m")
        Double elevationGainM,
        @JsonProperty("difficulty_score")
        Double difficultyScore,
        @JsonProperty("similarity_score")
        Double similarityScore
) {
}
