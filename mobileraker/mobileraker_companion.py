from asyncio import AbstractEventLoop, Lock
import asyncio
import base64
import logging
from typing import List, Optional
import time


import requests
from mobileraker.client.mobileraker_fcm_client import MobilerakerFcmClient
from mobileraker.client.moonraker_client import MoonrakerClient
from mobileraker.client.snapshot_client import SnapshotClient
from mobileraker.data.dtos.mobileraker.companion_meta_dto import CompanionMetaDataDto
from mobileraker.data.dtos.mobileraker.companion_request_dto import ContentDto, DeviceRequestDto, FcmRequestDto, LiveActivityContentDto, NotificationContentDto
from mobileraker.data.dtos.mobileraker.notification_config_dto import DeviceNotificationEntry
from mobileraker.data.dtos.moonraker.printer_snapshot import PrinterSnapshot
from mobileraker.service.data_sync_service import DataSyncService
from mobileraker.util.configs import CompanionLocalConfig, CompanionRemoteConfig

from mobileraker.util.functions import get_software_version, is_valid_uuid, normalized_progress_interval_reached
from mobileraker.util.i18n import translate, translate_replace_placeholders
from mobileraker.util.notification_placeholders import replace_placeholders


class MobilerakerCompanion:
    '''
        The companion class is the main coordinator between all logic in this project.
        It takes care of handling data updates, issuing new notifications, and updating any snapshot info
    '''

    def __init__(
            self,
            jrpc: MoonrakerClient,
            data_sync_service: DataSyncService,
            fcm_client: MobilerakerFcmClient,
            snapshot_client: SnapshotClient,
            printer_name: str,
            loop: AbstractEventLoop,
            companion_config: CompanionLocalConfig
    ) -> None:
        super().__init__()
        self._jrpc: MoonrakerClient = jrpc
        self._data_sync_service: DataSyncService = data_sync_service
        self._fcm_client: MobilerakerFcmClient = fcm_client
        self._snapshot_client: SnapshotClient = snapshot_client
        self.printer_name: str = printer_name
        self.loop: AbstractEventLoop = loop
        self.companion_config: CompanionLocalConfig = companion_config
        self.last_request: Optional[DeviceRequestDto] = None
        # TODO: Fetch this from a remote server for easier configuration :)
        self.remote_config = CompanionRemoteConfig()
        self._logger = logging.getLogger(
            f'mobileraker.{printer_name.replace(".","_")}')
        self._last_snapshot: Optional[PrinterSnapshot] = None
        self._last_apns_message: Optional[int] = None
        self._evaulate_noti_lock: Lock = Lock()

        self._jrpc.register_connection_listener(
            lambda d: self.loop.create_task(self._update_meta_data()) if d else None)
        self._data_sync_service.register_snapshot_listener(
            self._create_eval_task)

    async def start(self) -> None:
        await self._jrpc.connect()

    def _create_eval_task(self, snapshot: PrinterSnapshot) -> None:
        self.loop.create_task(self._evaluate_with_timeout(snapshot))

    async def _evaluate_with_timeout(self, snapshot: PrinterSnapshot) -> None:
        """
        This method starts the evaluation process with a timeout.
        It tries to acquire a lock before starting the evaluation.
        If the lock cannot be acquired within 60 seconds, or if the evaluation takes longer than 60 seconds,
        it logs a warning and releases the lock.
        """
        lock_acquired = False
        try:
            lock_acquired = await asyncio.wait_for(self._evaulate_noti_lock.acquire(), timeout=60)
            if lock_acquired:
                await asyncio.wait_for(self._evaluate(snapshot), timeout=60)
        except asyncio.TimeoutError:
            if lock_acquired:
                self._logger.warning('Evaluation task execution timed out after 60 seconds!')
            else:
                self._logger.warning('Evaluation task was unable to acquire lock after 60 seconds!')
        finally:
            if lock_acquired:
                self._evaulate_noti_lock.release()


    async def _evaluate(self, snapshot: PrinterSnapshot) -> None:
        # Limit evaluation to state changes and 5% increments(Later m117 can also trigger notifications, but might use other stuff)
        if not self._fulfills_evaluation_threshold(snapshot):
            return
        self._logger.info(
            'Snapshot passed threshold. LastSnap: %s, NewSnap: %s', self._last_snapshot, snapshot)
        self._last_snapshot = snapshot

        app_cfgs = await self._fetch_app_cfgs()

        device_requests: List[DeviceRequestDto] = []

        for cfg in app_cfgs:
            if not cfg.fcm_token:
                continue
            self._logger.info(
                'Evaluate for machineID %s, cfg.snap: %s, cfg.settings: %s', cfg.machine_id, cfg.snap, cfg.settings)
            notifications: List[ContentDto] = []

            state_noti = self._state_notification(cfg, snapshot)
            if state_noti is not None:
                notifications.append(state_noti)
                self._logger.info('StateNoti: %s - %s',
                                    state_noti.title, state_noti.body)

            progress_noti = self._progress_notification(cfg, snapshot)
            if progress_noti is not None:
                notifications.append(progress_noti)
                self._logger.info('ProgressNoti: %s - %s',
                                    progress_noti.title, progress_noti.body)

            m117_noti = self._custom_notification(cfg, snapshot, True)
            if m117_noti is not None:
                notifications.append(m117_noti)
                self._logger.info('M117Noti: %s - %s',
                                    m117_noti.title, m117_noti.body)

            gcode_response_noti = self._custom_notification(
                cfg, snapshot, False)
            if gcode_response_noti is not None:
                notifications.append(gcode_response_noti)
                self._logger.info('GCodeResponseNoti: %s - %s',
                                    gcode_response_noti.title, gcode_response_noti.body)

            live_activity_update = self._live_activity_update(
                cfg, snapshot)
            if live_activity_update is not None:
                notifications.append(live_activity_update)
                self._logger.info('LiveActivity (%s):  %s - %s',
                                    live_activity_update.token, live_activity_update.progress, live_activity_update.eta)

            self._logger.debug('Notifications for %s: %s',
                                cfg.fcm_token, notifications)

            self._logger.info('%i Notifications for machineID: %s: state: %s, proggress: %s, M117 %s, GcodeResponse: %s, LiveActivity: %s', len(
                notifications), cfg.machine_id, state_noti is not None, progress_noti is not None, m117_noti is not None, gcode_response_noti is not None, live_activity_update is not None)

            if notifications:
                # Set a webcam img to all DTOs if available
                dto = DeviceRequestDto(
                    printer_id=cfg.machine_id,
                    token=cfg.fcm_token,
                    notifcations=notifications
                )
                device_requests.append(dto)
            
            await self._update_app_snapshot(cfg, snapshot)
            await self._clean_up_apns(cfg, snapshot)

        self._take_webcam_image(device_requests)
        await self._push_and_clear_faulty(device_requests)
        self._logger.info('---- Completed Evaluations Task! ----')

    async def _update_meta_data(self) -> None:
        client_info = CompanionMetaDataDto(version=get_software_version())
        try:
            _, k_err = await self._jrpc.send_and_receive_method("server.database.post_item",
                                                                {"namespace": "mobileraker", "key": "fcm.client", "value": client_info.toJSON()})
            if k_err:
                self._logger.warning(
                    "Could not write companion meta into moonraker database, moonraker returned error %s", k_err)
            else:
                self._logger.info(
                    "Updated companion meta data in moonraker database")
        except (ConnectionError, asyncio.TimeoutError) as err:
            self._logger.warning(
                "Could not write companion meta into moonraker database, %s: %s", type(err), err)

    def _fulfills_evaluation_threshold(self, snapshot: PrinterSnapshot) -> bool:
        if self._last_snapshot is None:
            self._logger.info('No last snapshot available. Evaluating!')
            return True

        if self._last_snapshot.print_state != snapshot.print_state and not snapshot.is_timelapse_pause:
            self._logger.info('State changed. Evaluating!')
            return True

        if self._last_snapshot.m117_hash != snapshot.m117_hash and snapshot.m117 is not None and snapshot.m117.startswith('$MR$:'):
            self._logger.info('M117 changed. Evaluating!')
            return True

        if self._last_snapshot.gcode_response_hash != snapshot.gcode_response_hash and snapshot.gcode_response is not None and snapshot.gcode_response.startswith('MR_NOTIFY:'):
            self._logger.info('GcodeResponse changed. Evaluating!')
            return True


        if self._last_snapshot.eta is None and snapshot.eta is not None:
            self._logger.info('ETA is available. Evaluating!')
            return True

        # TODO: This is not yet working as intended. The eta does not trigger an evaluation with the current code!
        # Check if eta changed by more than 10 minutes and the last live activity update was more than 30 seconds ago
        # if (self._last_apns_message is not None and 
        #     (time.monotonic_ns() - self._last_apns_message) / 1e9 > 30 and
        #     self._last_snapshot.eta is not None and snapshot.eta is not None and 
        #     abs((self._last_snapshot.eta - snapshot.eta).seconds) > 600):
        #     self._logger.info('ETA changed by more than 10 minutes after 30 sec. Evaluating!')
        #     return True

        # Progress evaluation
        last_progress = self._last_snapshot.progress
        cur_progress = snapshot.progress

        if last_progress == cur_progress:
            return False

        if last_progress is None or cur_progress is None:
            self._logger.info('Progress is None. Evaluating!')
            return True

        if normalized_progress_interval_reached(last_progress, cur_progress, self.remote_config.increments):
            self._logger.info('Progress reached interval. Evaluating!')
            return True

        # Yes I know I can return on the last if, but I want to log the reason why it triggered an evaluation
        return False

    async def _fetch_app_cfgs(self) -> List[DeviceNotificationEntry]:
        try:
            response, k_error = await self._jrpc.send_and_receive_method("server.database.get_item",
                                                                         {"namespace": "mobileraker", "key": "fcm"})
            if k_error:
                self._logger.warning(
                    "Could not fetch app cfgs from moonraker, moonraker returned error %s", k_error)
                return []
            cfgs = []
            raw_cfgs = response["result"]["value"]
            for entry_id in raw_cfgs:
                if not is_valid_uuid(entry_id):
                    continue

                device_json = raw_cfgs[entry_id]
                if ('fcmToken' not in device_json):
                    await self._remove_old_fcm_cfg(entry_id)
                    continue
                cfg = DeviceNotificationEntry.fromJSON(
                    entry_id, device_json)
                cfgs.append(cfg)

            self._logger.info('Fetched %i app Cfgs!', len(cfgs))
            return cfgs
        except (ConnectionError, asyncio.TimeoutError) as err:
            self._logger.warning(
                "Could not fetch app cfgs from moonraker, %s: %s", type(err), err)
            return []

    async def _remove_old_fcm_cfg(self, machine_id: str) -> None:
        try:
            await self._jrpc.send_method(
                method="server.database.delete_item",
                params={"namespace": "mobileraker",
                        "key": f"fcm.{machine_id}"},
            )
        except (ConnectionError, asyncio.TimeoutError)as err:
            self._logger.warning(
                "Could not remove old fcm cfg for %s, %s", machine_id, err)

    def _state_notification(self, cfg: DeviceNotificationEntry, cur_snap: PrinterSnapshot) -> Optional[NotificationContentDto]:

        # check if we even need to issue a new notification!
        if cfg.snap.state == cur_snap.print_state:
            return None

        # only allow notifications of type error for the state transition printing -> error
        if cfg.snap.state != "printing" and cur_snap.print_state == "error":
            return None

        # check if new print state actually should issue a notification trough user configs
        if cur_snap.print_state not in cfg.settings.state_config:
            return None
                
        # Ignore paused state caused by timelapse plugin
        if cur_snap.is_timelapse_pause:
            return None

        # collect title and body to translate it
        title = translate_replace_placeholders(
            'state_title', cfg, cur_snap, self.companion_config)
        body = None
        if cur_snap.print_state == "printing":
            body = "state_printing_body"
        elif cur_snap.print_state == "paused":
            body = "state_paused_body"
        elif cur_snap.print_state == "complete":
            body = "state_completed_body"
        elif cur_snap.print_state == "error":
            body = "state_error_body"
        elif cur_snap.print_state == "standby":
            body = "state_standby_body"

        if title is None or body is None:
            raise AttributeError("Body or Title are none!")

        body = translate_replace_placeholders(
            body, cfg, cur_snap, self.companion_config)
        return NotificationContentDto(111, f'{cfg.machine_id}-statusUpdates', title, body)

    def _progress_notification(self, cfg: DeviceNotificationEntry, cur_snap: PrinterSnapshot) -> Optional[NotificationContentDto]:
        # If progress notifications are disabled, skip it!
        if cfg.settings.progress_config == -1:
            return None

        # only issue new progress notifications if the printer is printing, or paused
        # also skip if progress is at 100 since this notification is handled via the print state transition from printing to completed
        if cur_snap.print_state not in ["printing", "paused"] or cur_snap.progress is None or cur_snap.progress == 100:
            return None

        self._logger.info(
            'ProgressNoti preChecks: cfg.progress.config: %i - %i = %i < %i RESULT: %s',
            cur_snap.progress,
            cfg.snap.progress,
            cur_snap.progress - cfg.snap.progress,
            max(self.remote_config.increments, cfg.settings.progress_config),
            normalized_progress_interval_reached(cfg.snap.progress, cur_snap.progress, max(
                self.remote_config.increments, cfg.settings.progress_config))
        )

        # ensure the progress threshhold of the user's cfg is reached. If the cfg.snap is not yet printing also issue a notification
        if (cfg.snap.state in ["printing", "paused"]
                    and not normalized_progress_interval_reached(cfg.snap.progress, cur_snap.progress, max(self.remote_config.increments, cfg.settings.progress_config))
                ):
            return None

        title = translate_replace_placeholders(
            'print_progress_title', cfg, cur_snap, self.companion_config)
        body = translate_replace_placeholders(
            'print_progress_body', cfg, cur_snap, self.companion_config)
        return NotificationContentDto(222, f'{cfg.machine_id}-progressUpdates', title, body)

    def _live_activity_update(self, cfg: DeviceNotificationEntry, cur_snap: PrinterSnapshot) -> Optional[LiveActivityContentDto]:
        # If uuid is none or empty returm
        if cfg.apns is None or not cfg.apns.liveActivity:
            return None

        if cur_snap.progress is None:
            return None

        self._logger.info(
            'LiveActivityUpdate preChecks passed'
        )

        etaUpdate = self._last_snapshot is not None and \
                    self._last_snapshot.eta is not None and cur_snap.eta is not None and \
                    abs((self._last_snapshot.eta - cur_snap.eta).seconds) > 600

        # The live activity can be updted more frequent. Max however in 5 percent steps or if there was a state change
        if not normalized_progress_interval_reached(cfg.snap.progress_live_activity, cur_snap.progress, self.remote_config.increments) and cfg.snap.state == cur_snap.print_state and not etaUpdate:
            return None

        self._logger.info(
            'LiveActivityUpdate passed'
        )

        # Set the last apns message time to now
        self._last_apns_message = time.monotonic_ns()
        
        return LiveActivityContentDto(cfg.apns.liveActivity, cur_snap.progress, cur_snap.eta_seconds_utc, "update" if cur_snap.print_state in [
                                      "printing", "paused"] else "end")

    def _custom_notification(self, cfg: DeviceNotificationEntry, cur_snap: PrinterSnapshot, is_m117: bool) -> Optional[NotificationContentDto]:
        """
        Check if a custom notification should be issued.
        Args:
            cfg: The device notification configuration.
            cur_snap: The current printer snapshot.
            is_m117: Whether the notification is for an M117 message.

        Returns:
            The notification content, if any.
        """

        candidate = cur_snap.m117 if is_m117 else cur_snap.gcode_response
        prefix = '$MR$:' if is_m117 else 'MR_NOTIFY:'

        if not candidate:
            return None

        if not candidate.startswith(prefix):
            return None

        message = candidate[len(prefix):]
        if not message:
            return None

        # Check if this is a new notification
        if is_m117 and cfg.snap.m117 == cur_snap.m117_hash:
            return None
        elif not is_m117 and cfg.snap.gcode_response == cur_snap.gcode_response_hash:
            return None

        return self._construct_custom_notification(cfg, cur_snap, message)

    def _construct_custom_notification(self, cfg: DeviceNotificationEntry, cur_snap: PrinterSnapshot, message: str) -> Optional[NotificationContentDto]:
        split = message.split('|')

        has_title = (len(split) == 2)

        title = split[0].strip() if has_title else translate(
            cfg.language, 'm117_custom_title')
        title = replace_placeholders(
            title, cfg, cur_snap, self.companion_config)
        body = (split[1] if has_title else split[0]).strip()
        body = replace_placeholders(body, cfg, cur_snap, self.companion_config)

        self._logger.info(
            'Got M117/Custom: %s. This translated into: %s -  %s', message, title, body)

        return NotificationContentDto(333, f'{cfg.machine_id}-m117', title, body)

    def _take_webcam_image(self, dtos: List[DeviceRequestDto]) -> None:
        if not self.companion_config.include_snapshot:
            return
        if not dtos:
            return

        img_bytes = self._snapshot_client.take_snapshot()
        if img_bytes is None:
            return

        img = base64.b64encode(img_bytes).decode("ascii")

        for dto in dtos:
            for notification in dto.notifcations:
                if isinstance(notification, NotificationContentDto):
                    notification.image = img

    async def _push_and_clear_faulty(self, dtos: List[DeviceRequestDto]):
        try:
            if dtos:
                request = FcmRequestDto(dtos)
                response = self._fcm_client.push(request)
            # todo: remove faulty token lol
        except requests.exceptions.RequestException as err:
            self._logger.error(
                "Could not push notifications to mobileraker backend, %s: %s", type(err), err)

    async def _update_app_snapshot(self, cfg: DeviceNotificationEntry, printer_snap: PrinterSnapshot) -> None:
        try:
            last = cfg.snap

            progress_update = None
            if printer_snap.print_state not in ['printing', 'paused']:
                progress_update = 0
            elif (last.progress != printer_snap.progress
                  and printer_snap.progress is not None
                  and (normalized_progress_interval_reached(last.progress, printer_snap.progress, max(self.remote_config.increments, cfg.settings.progress_config))
                       or printer_snap.progress < last.progress)):
                progress_update = printer_snap.progress

            progress_live_activity_update = None
            if printer_snap.print_state not in ['printing', 'paused']:
                progress_live_activity_update = 0
            elif (last.progress_live_activity != printer_snap.progress
                  and printer_snap.progress is not None
                  and (normalized_progress_interval_reached(last.progress_live_activity, printer_snap.progress, self.remote_config.increments)
                       or printer_snap.progress < last.progress_live_activity)):
                progress_live_activity_update = printer_snap.progress

            updated = last.copy_with(
                state=printer_snap.print_state if last.state != printer_snap.print_state and not printer_snap.is_timelapse_pause else None,
                progress=progress_update,
                progress_live_activity=progress_live_activity_update,
                m117=printer_snap.m117_hash if last.m117 != printer_snap.m117_hash else None,
                gcode_response=printer_snap.gcode_response_hash if last.gcode_response != printer_snap.gcode_response_hash else None
            )

            if updated == last:
                self._logger.info(
                    "No snap update necessary for %s", cfg.machine_id)
                return

            self._logger.info('Updating snap in FCM Cfg for %s: %s',
                              cfg.machine_id, updated)
            response, k_err = await self._jrpc.send_and_receive_method("server.database.post_item",
                                                                       {"namespace": "mobileraker", "key": f"fcm.{cfg.machine_id}.snap", "value": updated.toJSON()})
            if k_err:
                self._logger.warning(
                    "Could not update snap in FCM Cfg for %s, moonraker returned error %s", cfg.machine_id, k_err)
            else:
                self._logger.info(
                    'Updated snap in FCM Cfg for %s: %s', cfg.machine_id, response)

        except (ConnectionError, asyncio.TimeoutError) as err:
            self._logger.warning(
                "Could not update snap in FCM Cfg for %s, %s: %s", cfg.machine_id, type(err), err)

    async def _clean_up_apns(self, cfg: DeviceNotificationEntry, printer_snap: PrinterSnapshot) -> None:
        if (cfg.apns is None):
            return
        if (printer_snap.print_state in ['printing', 'paused']):
            return
        machine_id = cfg.machine_id

        try:
            self._logger.info('Deleting APNS for %s', machine_id)
            _, k_err = await self._jrpc.send_and_receive_method(
                method="server.database.delete_item",
                params={"namespace": "mobileraker",
                        "key": f"fcm.{machine_id}.apns"},
            )
            if k_err:
                self._logger.warning(
                    "Could not remove apns for %s, moonraker returned error %s", machine_id, k_err)
            else:
                self._logger.info(
                    "Removed apns for %s", machine_id)
        except (ConnectionError, asyncio.TimeoutError)as err:
            self._logger.warning(
                "Could not remove apns for %s, %s", machine_id, err)
