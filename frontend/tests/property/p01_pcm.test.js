// Feature: nova-sonic-support-portal, Property 1: PCM conversion preserves audio structure

/**
 * Property 1 (design.md): for any Float32 audio input buffer at a supported
 * browser sample rate, the capture conversion produces 16-bit integer PCM at
 * 16 kHz where every sample lies within the Int16 range, the output length
 * equals the input length scaled by the resampling ratio (±1 frame), and
 * silence maps to silence.
 *
 * Validates: Requirements 1.1
 *
 * The property is exercised through the pure conversion functions exported
 * by `src/audio/pcm-worklet.js` — {@link downsampleFloat32} followed by
 * {@link float32ToInt16} — which is exactly the pipeline the AudioWorklet
 * processor runs per block. Generated buffers include out-of-range samples
 * (down to -4, up to +4) so the Int16 clamping path is exercised. NaN is
 * excluded (`noNaN: true`): microphone capture yields finite samples only,
 * and the property statement covers audio buffers, not signalling values.
 */

import { describe, expect, it } from 'vitest';
import fc from 'fast-check';

import {
  TARGET_SAMPLE_RATE,
  downsampleFloat32,
  float32ToInt16,
} from '../../src/audio/pcm-worklet.js';

/**
 * Sample rates (Hz) browsers commonly run AudioContexts at; the capture
 * worklet must convert from any of them to 16 kHz.
 * @type {import('fast-check').Arbitrary<number>}
 */
const supportedRateArb = fc.constantFrom(
  8000,
  16000,
  22050,
  24000,
  44100,
  48000,
  96000,
);

/**
 * Float32 capture buffers up to 4096 samples. Values span [-4, 4] so
 * out-of-nominal-range samples exercise clamping; NaN is excluded (see
 * file header).
 * @type {import('fast-check').Arbitrary<Float32Array>}
 */
const audioBufferArb = fc.float32Array({
  minLength: 0,
  maxLength: 4096,
  noNaN: true,
  min: Math.fround(-4),
  max: Math.fround(4),
});

describe('Property 1: PCM conversion preserves audio structure', () => {
  it('full pipeline yields integer samples within the Int16 range', () => {
    fc.assert(
      fc.property(audioBufferArb, supportedRateArb, (input, rate) => {
        const pcm = float32ToInt16(
          downsampleFloat32(input, rate, TARGET_SAMPLE_RATE),
        );
        const badIndex = pcm.findIndex(
          (sample) =>
            !Number.isInteger(sample) || sample < -32768 || sample > 32767,
        );
        expect(badIndex).toBe(-1);
      }),
      { numRuns: 100 },
    );
  });

  it('output length equals the input length scaled by the resampling ratio (±1 frame)', () => {
    fc.assert(
      fc.property(audioBufferArb, supportedRateArb, (input, rate) => {
        const downsampled = downsampleFloat32(input, rate, TARGET_SAMPLE_RATE);
        const pcm = float32ToInt16(downsampled);
        const expectedLength = Math.floor(
          (input.length * TARGET_SAMPLE_RATE) / rate,
        );
        expect(
          Math.abs(downsampled.length - expectedLength),
        ).toBeLessThanOrEqual(1);
        expect(pcm.length).toBe(downsampled.length);
      }),
      { numRuns: 100 },
    );
  });

  it('silence maps to silence for any buffer length and rate', () => {
    fc.assert(
      fc.property(fc.nat({ max: 4096 }), supportedRateArb, (length, rate) => {
        const silence = new Float32Array(length);
        const pcm = float32ToInt16(
          downsampleFloat32(silence, rate, TARGET_SAMPLE_RATE),
        );
        const nonZeroIndex = pcm.findIndex((sample) => sample !== 0);
        expect(nonZeroIndex).toBe(-1);
      }),
      { numRuns: 100 },
    );
  });

  it('float32ToInt16 alone preserves the input length', () => {
    fc.assert(
      fc.property(audioBufferArb, (input) => {
        expect(float32ToInt16(input).length).toBe(input.length);
      }),
      { numRuns: 100 },
    );
  });
});
