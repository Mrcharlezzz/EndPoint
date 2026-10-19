import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import tseslint from 'typescript-eslint';
import prettier from 'eslint-config-prettier';

const tsFiles = ['**/*.{ts,tsx}'];
const scopeToTs = (configs) => configs.map((c) => ({ ...c, files: tsFiles }));

export default [
  { ignores: ['dist', 'node_modules', 'src/api/schema.ts'] },
  { ...js.configs.recommended, files: tsFiles },
  ...scopeToTs(tseslint.configs.recommendedTypeChecked),
  ...scopeToTs(tseslint.configs.stylisticTypeChecked),
  {
    files: tsFiles,
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
      parserOptions: {
        project: ['./tsconfig.app.json', './tsconfig.node.json'],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/no-misused-promises': [
        'error',
        { checksVoidReturn: { attributes: false } },
      ],
    },
  },
  prettier,
];
