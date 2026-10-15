/**
 * @fileoverview Browser entry point mounting `RegionalDashboard` into `#root`.
 * @jsx React.createElement
 * @jsxFrag React.Fragment
 */

import * as React from 'react';
import * as reactDOM from 'react-dom';
import {RegionalDashboard} from './RegionalDashboard';

const rootElement = document.getElementById('root');
if (rootElement) {
  reactDOM.render(<RegionalDashboard />, rootElement);
}
