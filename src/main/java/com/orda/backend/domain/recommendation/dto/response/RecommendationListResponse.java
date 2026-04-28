package com.orda.backend.domain.recommendation.dto.response;

import java.util.List;

public record RecommendationListResponse(
        List<RecommendationItemResponse> recommendations
) {
}
