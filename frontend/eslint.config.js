import js from '@eslint/js';
import jsdoc from 'eslint-plugin-jsdoc';
import globals from 'globals';

/**
 * ESLint flat configuration for the frontend SPA.
 *
 * Enforces the recommended JavaScript rules plus JSDoc documentation rules:
 * every function, method, and class carries a JSDoc comment with a
 * description (Req 17.1).
 */
export default [
  js.configs.recommended,
  jsdoc.configs['flat/recommended'],
  {
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: { ...globals.browser },
    },
    rules: {
      'jsdoc/require-jsdoc': [
        'error',
        {
          require: {
            ArrowFunctionExpression: true,
            ClassDeclaration: true,
            ClassExpression: true,
            FunctionDeclaration: true,
            FunctionExpression: true,
            MethodDefinition: true,
          },
        },
      ],
      'jsdoc/require-description': 'error',
      'jsdoc/require-param-description': 'error',
      'jsdoc/require-returns-description': 'error',
    },
  },
  {
    files: ['public/sw.js'],
    languageOptions: { globals: { ...globals.serviceworker } },
  },
  {
    files: ['tests/**'],
    languageOptions: { globals: { ...globals.node, ...globals.browser } },
  },
];
