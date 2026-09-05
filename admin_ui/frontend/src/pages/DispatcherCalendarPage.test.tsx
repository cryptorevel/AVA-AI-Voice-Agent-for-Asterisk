// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import axios from 'axios';
import DispatcherCalendarPage from './DispatcherCalendarPage';

vi.mock('axios');
const mockedAxios = vi.mocked(axios, true);

const emptyCalendar = {
    organization_id: 'org-test',
    timezone: 'America/Vancouver',
    scheduling_enabled: false,
    business_hours: {},
    technicians: [],
    events: [],
    services: [{ id: 'repair', display_name: 'Repair', active: true, duration_minutes: 120 }],
};

describe('DispatcherCalendarPage', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockedAxios.get.mockResolvedValue({ data: emptyCalendar });
    });

    it('shows the fail-closed empty technician state without generating availability', async () => {
        render(<DispatcherCalendarPage />);
        expect(await screen.findByText('No production technicians are configured.')).toBeTruthy();
        expect(screen.getByText('Production availability is disabled.')).toBeTruthy();
        expect(mockedAxios.get).toHaveBeenCalledWith(
            '/api/tools/calendar',
            expect.objectContaining({
                params: expect.objectContaining({ active_only: true }),
            })
        );
        expect(mockedAxios.post).not.toHaveBeenCalled();
    });

    it('requests deterministic backend preview instead of calculating slots in the browser', async () => {
        mockedAxios.get
            .mockResolvedValueOnce({ data: emptyCalendar })
            .mockResolvedValueOnce({
                data: { status: 'configuration_required', slots: [], reason: 'No capacity' },
            });
        render(<DispatcherCalendarPage />);
        await screen.findByText('No production technicians are configured.');
        fireEvent.click(screen.getByRole('button', { name: 'Calculate real slots' }));
        await waitFor(() =>
            expect(mockedAxios.get).toHaveBeenLastCalledWith(
                '/api/tools/calendar/availability-preview',
                expect.objectContaining({
                    params: expect.objectContaining({ service_code: 'repair' }),
                })
            )
        );
        expect(await screen.findByText('Result: configuration_required')).toBeTruthy();
    });
});
