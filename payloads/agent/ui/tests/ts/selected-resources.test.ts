import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  encodeHeaderJson,
  withSelectedResources,
  withUserSkills,
} from '../../overrides/packages/ai-sdk-providers/src/selected-resources.ts';

const picks = encodeHeaderJson([{ kind: 'mcp_service', fqn: 'team.mcp.jira' }]);

describe('withSelectedResources', () => {
  it('sets custom_inputs.selected_resources and keeps the rest', () => {
    const body = JSON.stringify({
      input: [{ role: 'user', content: 'hi' }],
      custom_inputs: { other: 1 },
    });

    assert.deepEqual(JSON.parse(withSelectedResources(body, picks)), {
      input: [{ role: 'user', content: 'hi' }],
      custom_inputs: {
        other: 1,
        selected_resources: [{ kind: 'mcp_service', fqn: 'team.mcp.jira' }],
      },
    });
  });

  it('creates custom_inputs when absent', () => {
    const result = JSON.parse(withSelectedResources('{"input":[]}', picks));

    assert.equal(result.custom_inputs.selected_resources.length, 1);
  });

  it('leaves a non-JSON body unchanged', () => {
    assert.equal(withSelectedResources('not json', picks), 'not json');
  });

  it('leaves the body unchanged when the picks are not JSON', () => {
    assert.equal(withSelectedResources('{"input":[]}', '{oops'), '{"input":[]}');
  });
});

describe('withUserSkills', () => {
  it('sets custom_inputs.user_skills next to the picks', () => {
    const skills = encodeHeaderJson([{ fqn: 'lakebase:weekly', body: 'Steps.' }]);
    const body = withUserSkills(withSelectedResources('{"input":[]}', picks), skills);

    assert.deepEqual(Object.keys(JSON.parse(body).custom_inputs), [
      'selected_resources',
      'user_skills',
    ]);
  });

  it('carries a non-Latin-1 body through a header', () => {
    const skills = [{ fqn: 'lakebase:greet', body: '✨ Grüße → 你好' }];
    const headers = new Headers({ 'x-skills': encodeHeaderJson(skills) });

    const body = withUserSkills('{"input":[]}', headers.get('x-skills') ?? '');

    assert.deepEqual(JSON.parse(body).custom_inputs.user_skills, skills);
  });

  it('leaves the body unchanged when the header is not valid percent-encoding', () => {
    assert.equal(withUserSkills('{"input":[]}', '%E0%A4%A'), '{"input":[]}');
  });
});
