import { useEffect, useMemo, useRef, useState } from 'react';
import type { GeoJSONSource, Map as MapboxMap } from 'mapbox-gl/esm';
import type { FeatureCollection, Id } from '../types';

const token = window.__APP_CONFIG__?.mapboxAccessToken || import.meta.env.VITE_MAPBOX_ACCESS_TOKEN || '';

export function DatasetMap({ datasetId, data, onSelectId }: { datasetId: Id; data?: FeatureCollection; onSelectId: (id: Id) => void }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapboxMap | null>(null);
  const activeDataset = useRef<Id | null>(null);
  const currentDataset = useRef(datasetId);
  const currentData = useRef(data);
  const onSelectRef = useRef(onSelectId);
  currentDataset.current = datasetId;
  currentData.current = data;
  onSelectRef.current = onSelectId;
  const [mapError, setMapError] = useState('');
  const dataSignature = useMemo(() => JSON.stringify(data?.features?.map((feature) => feature.properties.id ?? feature.properties.image_id)), [data]);

  useEffect(() => {
    let cancelled = false;
    let instance: MapboxMap | null = null;
    if (!token || !container.current) return;
    void Promise.all([
      import('mapbox-gl/esm'),
      import('mapbox-gl/dist/mapbox-gl.css'),
    ]).then(([mapbox]) => {
      if (cancelled || !container.current) return;
      mapbox.setAccessToken(token);
      const mapInstance = new mapbox.Map({
        container: container.current,
        style: 'mapbox://styles/mapbox/streets-v12',
        center: [0, 20],
        zoom: 1.5,
        attributionControl: true,
      });
      instance = mapInstance;
      map.current = mapInstance;
      mapInstance.addControl(new mapbox.NavigationControl({ showCompass: false }), 'top-right');
      mapInstance.on('error', (event) => {
        if (event.error?.message && !mapInstance.isStyleLoaded()) setMapError('The map could not load. Check your Mapbox token and connection.');
      });
      mapInstance.on('load', () => {
        setMapError('');
        const initialData = currentData.current || emptyCollection();
        mapInstance.addSource('dataset-images', { type: 'geojson', data: initialData as never, cluster: true, clusterMaxZoom: 13, clusterRadius: 48 });
        mapInstance.addLayer({ id: 'image-clusters', type: 'circle', source: 'dataset-images', filter: ['has', 'point_count'], paint: { 'circle-color': '#306b5b', 'circle-radius': ['step', ['get', 'point_count'], 19, 12, 24, 40, 30], 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2 } });
        mapInstance.addLayer({ id: 'cluster-count', type: 'symbol', source: 'dataset-images', filter: ['has', 'point_count'], layout: { 'text-field': ['get', 'point_count_abbreviated'], 'text-font': ['DIN Offc Pro Medium', 'Arial Unicode MS Bold'], 'text-size': 12 }, paint: { 'text-color': '#ffffff' } });
        mapInstance.addLayer({ id: 'image-points', type: 'circle', source: 'dataset-images', filter: ['!', ['has', 'point_count']], paint: { 'circle-color': '#e08a52', 'circle-radius': 7, 'circle-stroke-width': 2, 'circle-stroke-color': '#ffffff' } });
        mapInstance.on('click', 'image-clusters', (event) => {
          const feature = mapInstance.queryRenderedFeatures(event.point, { layers: ['image-clusters'] })[0];
          const clusterId = feature?.properties?.cluster_id;
          const source = mapInstance.getSource('dataset-images') as GeoJSONSource;
          if (clusterId != null && feature?.geometry.type === 'Point') {
            const center = feature.geometry.coordinates as [number, number];
            source.getClusterExpansionZoom(Number(clusterId), (error: Error | null | undefined, zoom: number | null | undefined) => {
              if (!error && typeof zoom === 'number') mapInstance.easeTo({ center, zoom });
            });
          }
        });
        mapInstance.on('click', 'image-points', (event) => {
          const feature = mapInstance.queryRenderedFeatures(event.point, { layers: ['image-points'] })[0];
          const properties = feature?.properties;
          const id = properties?.image_id ?? properties?.id;
          if (id != null) onSelectRef.current(id);
        });
        mapInstance.on('mouseenter', 'image-points', () => { mapInstance.getCanvas().style.cursor = 'pointer'; });
        mapInstance.on('mouseleave', 'image-points', () => { mapInstance.getCanvas().style.cursor = ''; });
        mapInstance.on('mouseenter', 'image-clusters', () => { mapInstance.getCanvas().style.cursor = 'pointer'; });
        mapInstance.on('mouseleave', 'image-clusters', () => { mapInstance.getCanvas().style.cursor = ''; });
        fitToDataset(mapInstance, currentDataset.current, initialData, activeDataset);
      });
    }).catch(() => setMapError('The map could not be loaded. Refresh the page and try again.'));
    return () => {
      cancelled = true;
      instance?.remove();
      map.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !data || !instance.getSource('dataset-images')) return;
    const source = instance.getSource('dataset-images') as GeoJSONSource;
    source.setData(data as never);
    // Initial fit happens in the map load handler. Later changes fit only when
    // the user switches datasets; polling new image features preserves the view.
    if (String(activeDataset.current) !== String(datasetId)) {
      // This uses Mapbox's bounds implementation from the loaded map instance.
      fitToDataset(instance, datasetId, data, activeDataset);
    }
  }, [dataSignature, datasetId, data]);

  if (!token) return <div className="map-unavailable"><div className="map-unavailable-art"><div className="map-unavailable-ring ring-one"/><div className="map-unavailable-ring ring-two"/><span>⌖</span></div><strong>Mapbox token needed</strong><p>Add <code>MAPBOX_ACCESS_TOKEN</code> to the frontend environment to view image locations on the map.</p></div>;
  return <div className="map-wrap"><div ref={container} className="map-container" aria-label="Map showing image locations" />{mapError && <div className="map-error">{mapError}</div>}{data && data.features.length === 0 && <div className="map-empty-note">GPS locations will appear here after processing.</div>}</div>;
}

function fitToDataset(map: MapboxMap, datasetId: Id, data: FeatureCollection, activeDataset: { current: Id | null }) {
  const points = data.features.filter((feature) => feature.geometry.type === 'Point');
  if (points.length === 1) map.flyTo({ center: points[0].geometry.coordinates, zoom: 10, duration: 500 });
  else if (points.length > 1) {
    const longitudes = points.map((feature) => feature.geometry.coordinates[0]);
    const latitudes = points.map((feature) => feature.geometry.coordinates[1]);
    map.fitBounds([[Math.min(...longitudes), Math.min(...latitudes)], [Math.max(...longitudes), Math.max(...latitudes)]], { padding: 52, maxZoom: 12, duration: 500 });
  }
  activeDataset.current = datasetId;
}

function emptyCollection(): FeatureCollection { return { type: 'FeatureCollection', features: [] }; }
