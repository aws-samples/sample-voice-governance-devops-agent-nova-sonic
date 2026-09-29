/**
 * Unit tests for chat-style transcript ordering (Req 1.4).
 *
 * The transcript is one chronological thread, so an assistant reply always
 * renders directly beneath the engineer turn it answers. Role is conveyed
 * by the entry's `data-role` (which the stylesheet turns into left/right
 * alignment) and its Bootstrap colours — never by a separate container, an
 * earlier two-pane layout that forced the reader to correlate turns across
 * columns by timestamp.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import { ROLE_PRESENTATION, appendTranscript } from '../../src/ui/transcript.js';

/**
 * Creates the transcript thread with its placeholder attached.
 * @returns {HTMLElement} The thread element.
 */
function createThread() {
  document.body.innerHTML = `
    <div id="transcript">
      <p data-transcript-placeholder>Your conversation will appear here.</p>
    </div>`;
  return document.getElementById('transcript');
}

/**
 * Reads the thread's entries as `[role, text]` pairs in DOM order.
 * @param {HTMLElement} thread - The transcript thread.
 * @returns {Array<[string, string]>} Ordered role/text pairs.
 */
function threadOrder(thread) {
  return [...thread.querySelectorAll('[data-transcript-entry]')].map((el) => [
    el.dataset.role,
    el.querySelector('p').textContent,
  ]);
}

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('chat-style transcript thread', () => {
  it('keeps turns in conversational order, reply under request', () => {
    const thread = createThread();

    appendTranscript({ role: 'USER', text: 'list all ec2 instances' });
    appendTranscript({ role: 'ASSISTANT', text: 'you have three instances' });
    appendTranscript({ role: 'USER', text: 'why is i-0abc123 unhealthy?' });
    appendTranscript({ role: 'ASSISTANT', text: 'its target group check fails' });

    expect(threadOrder(thread)).toEqual([
      ['user', 'list all ec2 instances'],
      ['assistant', 'you have three instances'],
      ['user', 'why is i-0abc123 unhealthy?'],
      ['assistant', 'its target group check fails'],
    ]);
  });

  it('renders every turn into the one thread', () => {
    const thread = createThread();

    appendTranscript({ role: 'USER', text: 'first' });
    appendTranscript({ role: 'ASSISTANT', text: 'second' });

    expect(thread.querySelectorAll('[data-transcript-entry]')).toHaveLength(2);
    // No per-role containers exist any more.
    expect(document.getElementById('transcript-user')).toBeNull();
    expect(document.getElementById('transcript-assistant')).toBeNull();
  });

  it('clears the placeholder on the first turn only', () => {
    const thread = createThread();

    appendTranscript({ role: 'USER', text: 'hello' });
    expect(thread.querySelector('[data-transcript-placeholder]')).toBeNull();

    appendTranscript({ role: 'ASSISTANT', text: 'hi' });
    expect(threadOrder(thread)).toHaveLength(2);
  });

  it('marks each role so the stylesheet can align and colour it', () => {
    const thread = createThread();

    appendTranscript({ role: 'USER', text: 'mine' });
    appendTranscript({ role: 'ASSISTANT', text: 'theirs' });
    const [userEl, assistantEl] = thread.querySelectorAll(
      '[data-transcript-entry]',
    );

    expect(userEl.dataset.role).toBe('user');
    expect(assistantEl.dataset.role).toBe('assistant');
    for (const cls of ROLE_PRESENTATION.user.entryClass.split(' ')) {
      expect(userEl.classList.contains(cls)).toBe(true);
    }
    for (const cls of ROLE_PRESENTATION.assistant.entryClass.split(' ')) {
      expect(assistantEl.classList.contains(cls)).toBe(true);
    }
    // The two roles are visually distinct.
    expect(ROLE_PRESENTATION.user.entryClass).not.toBe(
      ROLE_PRESENTATION.assistant.entryClass,
    );
  });

  it('returns null when the page has no transcript markup', () => {
    expect(appendTranscript({ role: 'USER', text: 'nowhere' })).toBeNull();
  });
});
