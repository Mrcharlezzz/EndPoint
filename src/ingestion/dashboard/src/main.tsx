import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App.tsx';
import './styles/index.css';

const mount = document.getElementById('root');
if (!mount) throw new Error('#root not found');

createRoot(mount).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
