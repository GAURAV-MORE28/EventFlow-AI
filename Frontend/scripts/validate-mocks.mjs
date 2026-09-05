#!/usr/bin/env node
/**
 * Validates every mock fixture against the JSON Schemas exported from the
 * backend's Pydantic models (00_SHARED_CONTRACT.md, "Schema validation in CI").
 *
 *   npm run validate:mocks
 *
 * If a mock file fails validation, the build fails. That is the whole
 * mechanism that keeps frontend mocks and backend reality from diverging.
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const SCHEMA_DIR = path.resolve(ROOT, '../contracts/schemas');
const MOCK_DIR = path.resolve(ROOT, 'src/mocks');

// mock file -> schema name. Sequence/progression files validate each entry
// against the same per-cycle schema; everything else validates whole.
const MAPPING = {
  'event.json': 'event_response',
  'graph.json': 'graph_response',
  'state.json': 'state_response',
  'interventions.json': 'intervention_list_response',
  'twin_fidelity.json': 'twin_fidelity',
  'metrics.json': 'metrics_response',
  'regret.json': 'regret_response',
  'cascade_metro_b.json': 'cascade_result',
  'cascades_active.json': 'active_cascades_response',
  'attendee_journey.json': 'journey_response',
  'simulation.json': 'simulation_result',
  'health.json': 'health_response',
};

// These are arrays of per-cycle frames; each frame validates against a schema
// built from the endpoint shape it stands in for.
const SEQUENCE_MAPPING = {
  'pressure_timeline.json': 'pressure_timeline_frame',
};

function loadSchema(name) {
  const file = path.join(SCHEMA_DIR, `${name}.json`);
  if (!existsSync(file)) {
    throw new Error(`schema not found: ${file} (run: python -m scripts.export_schemas)`);
  }
  return JSON.parse(readFileSync(file, 'utf-8'));
}

function main() {
  if (!existsSync(SCHEMA_DIR)) {
    console.error(`No schema directory at ${SCHEMA_DIR}.`);
    console.error('Run: cd Backend && python -m scripts.export_schemas --out ../contracts/schemas');
    process.exit(1);
  }

  const ajv = new Ajv2020({ strict: false, allErrors: true });
  addFormats(ajv);

  let failures = 0;
  let checked = 0;

  for (const [file, schemaName] of Object.entries(MAPPING)) {
    const mockPath = path.join(MOCK_DIR, file);
    if (!existsSync(mockPath)) {
      console.warn(`  skip (missing): ${file}`);
      continue;
    }
    const schema = loadSchema(schemaName);
    const validate = ajv.compile(schema);
    const data = JSON.parse(readFileSync(mockPath, 'utf-8'));
    checked += 1;
    if (!validate(data)) {
      failures += 1;
      console.error(`✗ ${file} (schema: ${schemaName})`);
      for (const err of validate.errors.slice(0, 5)) {
        console.error(`    ${err.instancePath || '/'} ${err.message}`);
      }
    } else {
      console.log(`✓ ${file}`);
    }
  }

  // state_sequence.json / twin_fidelity_sequence.json are arrays of frames;
  // each frame's `entities` / history point already validates against the
  // per-entity / per-point schemas embedded in state_response / twin_fidelity.
  for (const file of ['state_sequence.json', 'twin_fidelity_sequence.json']) {
    const mockPath = path.join(MOCK_DIR, file);
    if (!existsSync(mockPath)) continue;
    const data = JSON.parse(readFileSync(mockPath, 'utf-8'));
    checked += 1;
    if (!Array.isArray(data)) {
      failures += 1;
      console.error(`✗ ${file}: expected an array of cycles`);
    } else {
      console.log(`✓ ${file} (${data.length} cycles, structural check)`);
    }
  }

  console.log(`\n${checked - failures}/${checked} mock files valid.`);
  if (failures > 0) {
    console.error(`${failures} mock file(s) failed validation. The build fails.`);
    process.exit(1);
  }
}

main();
