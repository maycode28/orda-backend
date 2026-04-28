package com.orda.backend.domain.recommendation.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.orda.backend.domain.recommendation.dto.response.RecommendationItemResponse;
import com.orda.backend.domain.recommendation.dto.response.RecommendationListResponse;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.TimeUnit;

@Slf4j
@Service
@RequiredArgsConstructor
public class RecommendationService {

    private final ObjectMapper objectMapper;
    private final JdbcTemplate jdbcTemplate;

    @Value("${recommendation.python-executable:python3}")
    private String pythonExecutable;

    @Value("${recommendation.script-path:python/scripts/recommendation/recommend.py}")
    private String scriptPath;

    @Value("${recommendation.timeout-seconds:8}")
    private long timeoutSeconds;

    @Value("${spring.datasource.url}")
    private String datasourceUrl;

    @Value("${spring.datasource.username}")
    private String datasourceUsername;

    @Value("${spring.datasource.password}")
    private String datasourcePassword;

    public RecommendationListResponse recommend(Long userId, int topN) {
        int normalizedTopN = Math.max(1, Math.min(topN, 20));

        try {
            List<RecommendationItemResponse> recommendations = runPython(userId, normalizedTopN);
            return new RecommendationListResponse(recommendations);
        } catch (Exception e) {
            log.warn("추천 ML 스크립트 실패 - fallback 추천 반환. userId={}, topN={}", userId, normalizedTopN, e);
            return new RecommendationListResponse(loadFallbackRecommendations(Math.min(normalizedTopN, 3)));
        }
    }

    private List<RecommendationItemResponse> runPython(Long userId, int topN) throws Exception {
        ProcessBuilder processBuilder = new ProcessBuilder(
                pythonExecutable,
                scriptPath,
                "--user_id",
                String.valueOf(userId),
                "--top_n",
                String.valueOf(topN)
        );
        processBuilder.redirectErrorStream(false);
        processBuilder.environment().putIfAbsent("DATABASE_URL", toPythonDatabaseUrl(datasourceUrl));
        processBuilder.environment().putIfAbsent("DB_USERNAME", datasourceUsername);
        processBuilder.environment().putIfAbsent("DB_PASSWORD", datasourcePassword);

        Process process = processBuilder.start();
        boolean finished = process.waitFor(timeoutSeconds, TimeUnit.SECONDS);

        String stdout = read(process.inputReader(StandardCharsets.UTF_8));
        String stderr = read(process.errorReader(StandardCharsets.UTF_8));

        if (!finished) {
            process.destroyForcibly();
            throw new IllegalStateException("추천 스크립트 timeout: " + Duration.ofSeconds(timeoutSeconds));
        }

        if (process.exitValue() != 0) {
            throw new IllegalStateException("추천 스크립트 실패: " + stderr);
        }

        return objectMapper.readValue(stdout, new TypeReference<List<RecommendationItemResponse>>() {
        });
    }

    private String read(BufferedReader reader) throws Exception {
        StringBuilder builder = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            builder.append(line);
        }
        return builder.toString();
    }

    private String toPythonDatabaseUrl(String url) {
        if (url != null && url.startsWith("jdbc:")) {
            return url.substring("jdbc:".length());
        }
        return url;
    }

    private List<RecommendationItemResponse> loadFallbackRecommendations(int topN) {
        try {
            List<RecommendationItemResponse> monthlyPopular = loadMonthlyPopularRecommendations(topN);
            if (!monthlyPopular.isEmpty()) {
                return monthlyPopular;
            }
            return loadCourseCatalogFallback(topN);
        } catch (DataAccessException e) {
            log.warn("추천 fallback 조회 실패 - 빈 추천 반환. topN={}", topN, e);
            return List.of();
        }
    }

    private List<RecommendationItemResponse> loadMonthlyPopularRecommendations(int topN) {
        String sql = """
                WITH course_features AS (
                    SELECT
                        e.source_gpx AS trail_id,
                        COALESCE(NULLIF(regexp_replace(e.source_gpx, '\\.gpx$', '', 'i'), ''), e.source_gpx) AS name,
                        COALESCE(SUM(e.distance_m), 0) / 1000.0 AS distance_km,
                        COALESCE(SUM(GREATEST(COALESCE(e.elevation_diff_m, 0), 0)), 0) AS elevation_gain_m,
                        COALESCE(
                            SUM(COALESCE(e.difficulty_score, 0) * COALESCE(NULLIF(e.distance_m, 0), 1))
                            / NULLIF(SUM(COALESCE(NULLIF(e.distance_m, 0), 1)), 0),
                            AVG(e.difficulty_score),
                            0
                        ) AS difficulty_score
                    FROM trail_edges e
                    WHERE e.source_gpx IS NOT NULL
                      AND e.source_gpx <> ''
                      AND e.distance_m IS NOT NULL
                    GROUP BY e.source_gpx
                    HAVING SUM(e.distance_m) > 0
                ),
                matched_points AS (
                    SELECT
                        s.session_id,
                        nearest.source_gpx AS trail_id
                    FROM hiking_sessions s
                    JOIN gps_tracks g ON g.session_id = s.session_id
                    JOIN LATERAL (
                        SELECT e.source_gpx
                        FROM trail_edges e
                        WHERE e.source_gpx IS NOT NULL
                          AND e.source_gpx <> ''
                          AND e.geom IS NOT NULL
                          AND ST_DWithin(
                              e.geom::geography,
                              ST_SetSRID(
                                  ST_MakePoint(
                                      COALESCE(g.snapped_longitude, g.raw_longitude),
                                      COALESCE(g.snapped_latitude, g.raw_latitude)
                                  ),
                                  4326
                              )::geography,
                              50
                          )
                        ORDER BY e.geom <->
                            ST_SetSRID(
                                ST_MakePoint(
                                    COALESCE(g.snapped_longitude, g.raw_longitude),
                                    COALESCE(g.snapped_latitude, g.raw_latitude)
                                ),
                                4326
                            )
                        LIMIT 1
                    ) nearest ON TRUE
                    WHERE s.status = 'COMPLETED'
                      AND s.started_at >= date_trunc('month', CURRENT_DATE)
                ),
                session_courses AS (
                    SELECT session_id, trail_id
                    FROM (
                        SELECT
                            session_id,
                            trail_id,
                            ROW_NUMBER() OVER (
                                PARTITION BY session_id
                                ORDER BY COUNT(*) DESC, trail_id
                            ) AS rn
                        FROM matched_points
                        GROUP BY session_id, trail_id
                    ) ranked
                    WHERE rn = 1
                ),
                monthly_popularity AS (
                    SELECT
                        sc.trail_id,
                        COUNT(*) AS hiking_count,
                        COUNT(DISTINCT sv.session_id) AS summit_completion_count
                    FROM session_courses sc
                    LEFT JOIN summit_verifications sv ON sv.session_id = sc.session_id
                    GROUP BY sc.trail_id
                )
                SELECT
                    cf.trail_id,
                    cf.name,
                    cf.distance_km,
                    cf.elevation_gain_m,
                    cf.difficulty_score
                FROM course_features cf
                JOIN monthly_popularity mp ON mp.trail_id = cf.trail_id
                ORDER BY mp.hiking_count DESC,
                         mp.summit_completion_count DESC,
                         cf.distance_km DESC,
                         cf.trail_id
                LIMIT ?
                """;

        return mapRecommendationRows(sql, topN);
    }

    private List<RecommendationItemResponse> loadCourseCatalogFallback(int topN) {
        String sql = """
                SELECT
                    e.source_gpx AS trail_id,
                    COALESCE(NULLIF(regexp_replace(e.source_gpx, '\\.gpx$', '', 'i'), ''), e.source_gpx) AS name,
                    COALESCE(SUM(e.distance_m), 0) / 1000.0 AS distance_km,
                    COALESCE(SUM(GREATEST(COALESCE(e.elevation_diff_m, 0), 0)), 0) AS elevation_gain_m,
                    COALESCE(
                        SUM(COALESCE(e.difficulty_score, 0) * COALESCE(NULLIF(e.distance_m, 0), 1))
                        / NULLIF(SUM(COALESCE(NULLIF(e.distance_m, 0), 1)), 0),
                        AVG(e.difficulty_score),
                        0
                    ) AS difficulty_score,
                    COUNT(*) AS edge_count
                FROM trail_edges e
                WHERE e.source_gpx IS NOT NULL
                  AND e.source_gpx <> ''
                  AND e.distance_m IS NOT NULL
                GROUP BY e.source_gpx
                HAVING SUM(e.distance_m) > 0
                ORDER BY edge_count DESC, distance_km DESC, difficulty_score DESC, trail_id
                LIMIT ?
                """;

        return mapRecommendationRows(sql, topN);
    }

    private List<RecommendationItemResponse> mapRecommendationRows(String sql, int topN) {
        return jdbcTemplate.query(sql, (rs, rowNum) -> new RecommendationItemResponse(
                rs.getString("trail_id"),
                rs.getString("name"),
                rs.getDouble("distance_km"),
                rs.getDouble("elevation_gain_m"),
                rs.getDouble("difficulty_score"),
                0.0
        ), topN);
    }
}
