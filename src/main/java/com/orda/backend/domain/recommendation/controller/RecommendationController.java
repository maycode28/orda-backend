package com.orda.backend.domain.recommendation.controller;

import com.orda.backend.common.response.ApiResponse;
import com.orda.backend.domain.recommendation.dto.response.RecommendationListResponse;
import com.orda.backend.domain.recommendation.service.RecommendationService;
import com.orda.backend.security.CustomUserDetails;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/recommendations")
@RequiredArgsConstructor
public class RecommendationController {

    private final RecommendationService recommendationService;

    @GetMapping
    public ResponseEntity<ApiResponse<RecommendationListResponse>> recommend(
            @AuthenticationPrincipal CustomUserDetails userDetails,
            @RequestParam(name = "top_n", defaultValue = "5") int topN
    ) {
        RecommendationListResponse response = recommendationService.recommend(userDetails.getUserId(), topN);
        return ResponseEntity.ok(ApiResponse.success("추천 코스 조회 성공", response));
    }
}
