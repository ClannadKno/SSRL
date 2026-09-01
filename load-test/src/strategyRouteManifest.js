const path = require('path');

const { expectedOverlayStates, expectedPrimaryState } = require('./scenarioClassification');

const DEFAULT_MANIFEST_PATH = path.join(
  __dirname,
  '..',
  '..',
  'services',
  'strategy_route_manifest.json'
);
const DEFAULT_MANIFEST = require(DEFAULT_MANIFEST_PATH);
const STRATEGY_ID_PATTERN = /^(?:EA|EE|ER|SS|OI)-\d{3}$/;

function unique(items) {
  return [...new Set((items || []).filter(Boolean).map((item) => String(item)))];
}

function routePool(route) {
  if (!route) return [];
  return unique([
    ...(Array.isArray(route.primary_strategy_ids) ? route.primary_strategy_ids : []),
    ...(Array.isArray(route.backup_strategy_ids) ? route.backup_strategy_ids : [])
  ]);
}

function manifestRoutes(manifest = DEFAULT_MANIFEST) {
  return manifest && manifest.routes && typeof manifest.routes === 'object'
    ? manifest.routes
    : {};
}

function routeSourceVersion(manifest = DEFAULT_MANIFEST) {
  return manifest && manifest.version ? String(manifest.version) : null;
}

function routeForScenario(item, manifest = DEFAULT_MANIFEST) {
  const routes = manifestRoutes(manifest);
  const primaryState = expectedPrimaryState(item);
  const overlayStates = expectedOverlayStates(item);
  const primaryRoute = primaryState ? routes[primaryState] || null : null;
  const overlayRoutes = overlayStates.map((state) => ({
    state,
    route: routes[state] || null
  }));
  const primaryAllowedStrategyIds = routePool(primaryRoute);
  const overlayAllowedStrategyIds = unique(
    overlayRoutes.flatMap((entry) => routePool(entry.route))
  );
  const combinedRuntimeAllowedStrategyIds = unique([
    ...primaryAllowedStrategyIds,
    ...overlayAllowedStrategyIds
  ]);
  const effectiveOverlay = overlayRoutes.find((entry) => entry.route) || null;
  const effectiveRoute = effectiveOverlay ? effectiveOverlay.route : primaryRoute;

  return {
    primaryState: primaryState ? String(primaryState) : null,
    overlayStates,
    primaryRoute,
    overlayRoutes,
    primaryAllowedStrategyIds,
    overlayAllowedStrategyIds,
    combinedRuntimeAllowedStrategyIds,
    runtimeAllowedStrategyIds: combinedRuntimeAllowedStrategyIds,
    routeMode: effectiveRoute && effectiveRoute.route_mode
      ? String(effectiveRoute.route_mode)
      : null,
    interventionMode: effectiveRoute && effectiveRoute.intervention_mode
      ? String(effectiveRoute.intervention_mode)
      : null,
    primaryRouteMode: primaryRoute && primaryRoute.route_mode
      ? String(primaryRoute.route_mode)
      : null,
    overlayRouteModes: overlayRoutes
      .filter((entry) => entry.route)
      .map((entry) => ({ state: entry.state, routeMode: String(entry.route.route_mode || '') })),
    routeSourceVersion: routeSourceVersion(manifest),
    routePresent: Boolean(primaryRoute || effectiveOverlay),
    effectiveRouteState: effectiveOverlay ? effectiveOverlay.state : primaryState
  };
}

function expectedPrimaryStrategyIdForScenario(item, manifest = DEFAULT_MANIFEST) {
  const explicit = item && (
    item.expected_primary_strategy_id ||
    item.expectedPrimaryStrategyId
  );
  if (explicit) return String(explicit);
  const route = routeForScenario(item, manifest);
  const primaryIds = route.primaryRoute && route.primaryRoute.primary_strategy_ids;
  return Array.isArray(primaryIds) && primaryIds.length
    ? String(primaryIds[0])
    : null;
}

function selectedStrategyIdForScenario(item) {
  const value = item && (
    item.selected_strategy_id ||
    item.selectedStrategyId ||
    item.preferredStrategyId ||
    item.preferred_strategy_id
  );
  if (value) return String(value);
  const inhibition = item && (item.inhibitionStrategyId || item.inhibition_strategy_id);
  return inhibition ? String(inhibition) : null;
}

function isStrategyId(value) {
  return STRATEGY_ID_PATTERN.test(String(value || ''));
}

function isStrategyAllowedForScenario(item, strategyId, manifest = DEFAULT_MANIFEST) {
  const value = String(strategyId || '').trim();
  return Boolean(value) && routeForScenario(item, manifest)
    .combinedRuntimeAllowedStrategyIds.includes(value);
}

function allManifestStrategyIds(manifest = DEFAULT_MANIFEST) {
  return unique(Object.values(manifestRoutes(manifest)).flatMap(routePool));
}

module.exports = {
  DEFAULT_MANIFEST,
  DEFAULT_MANIFEST_PATH,
  allManifestStrategyIds,
  expectedPrimaryStrategyIdForScenario,
  isStrategyAllowedForScenario,
  isStrategyId,
  manifestRoutes,
  routeForScenario,
  routePool,
  routeSourceVersion,
  selectedStrategyIdForScenario,
  unique
};
