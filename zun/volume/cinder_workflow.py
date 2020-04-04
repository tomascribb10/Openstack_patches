#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import six

from cinderclient import exceptions as cinder_exception
from os_brick import exception as os_brick_exception
from os_brick.initiator import connector as brick_connector
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import excutils

from zun.common import exception
from zun.common.i18n import _
import zun.conf
from zun import objects
from zun.volume import cinder_api as cinder


LOG = logging.getLogger(__name__)

CONF = zun.conf.CONF


def get_volume_connector_properties():
    """Wrapper to automatically set root_helper in brick calls.

    :param multipath: A boolean indicating whether the connector can
                      support multipath.
    :param enforce_multipath: If True, it raises exception when multipath=True
                              is specified but multipathd is not running.
                              If False, it falls back to multipath=False
                              when multipathd is not running.
    """

    return brick_connector.get_connector_properties(
        None,
        CONF.my_block_storage_ip,
        CONF.volume.use_multipath,
        enforce_multipath=True,
        host=CONF.host)


def get_volume_connector(protocol, driver=None,
                         device_scan_attempts=3,
                         *args, **kwargs):
    """Wrapper to get a brick connector object.

    This automatically populates the required protocol as well
    as the root_helper needed to execute commands.
    """

    if protocol.upper() == "RBD":
        kwargs['do_local_attach'] = True
    return brick_connector.InitiatorConnector.factory(
        protocol, None,
        driver=driver,
        use_multipath=CONF.volume.use_multipath,
        device_scan_attempts=device_scan_attempts,
        *args, **kwargs)


class CinderWorkflow(object):

    def __init__(self, context):
        self.context = context
        self.cinder_api = cinder.CinderAPI(self.context)

    def attach_volume(self, volmap):
        try:
            return self._do_attach_volume(self.cinder_api, volmap)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception("Failed to attach volume %(volume_id)s",
                              {'volume_id': volmap.cinder_volume_id})
                self.cinder_api.unreserve_volume(volmap.cinder_volume_id)

    def _do_attach_volume(self, cinder_api, volmap):
        volume_id = volmap.cinder_volume_id
        container_uuid = volmap.container_uuid

        cinder_api.reserve_volume(volume_id)
        conn_info = cinder_api.initialize_connection(
            volume_id,
            get_volume_connector_properties())
        LOG.info("Get connection information %s", conn_info)

        try:
            ##PatchNS
            volumen = cinder_api.get(volume_id)
            volumen_type = volumen.volume_type + '.infraestructura.com.ar'
            if volumen_type == volmap.host:
                device_info = {}
                prefijo_device_info = '/dev/zvol/Storage01/volume-'
                device_info['path'] = prefijo_device_info + volume_id
            else:
                device_info = self._connect_volume(conn_info)
            LOG.info("Get device_info after connect to "
                     "volume %s", device_info)
        except Exception:
            with excutils.save_and_reraise_exception():
                cinder_api.terminate_connection(
                    volume_id, get_volume_connector_properties())

        conn_info['data']['device_path'] = device_info['path']
        mountpoint = device_info['path']
        try:
            volmap.connection_info = jsonutils.dumps(conn_info)
        except TypeError:
            pass
        # NOTE(hongbin): save connection_info in the database
        # before calling cinder_api.attach because the volume status
        # will go to 'in-use' then caller immediately try to detach
        # the volume and connection_info is required for detach.
        volmap.save()

        try:
            cinder_api.attach(volume_id=volume_id,
                              mountpoint=mountpoint,
                              hostname=CONF.host,
                              container_uuid=container_uuid)
            LOG.info("Attach volume to this server successfully")
        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    self._disconnect_volume(conn_info)
                except os_brick_exception.VolumeDeviceNotFound as exc:
                    LOG.warning('Ignoring VolumeDeviceNotFound: %s', exc)

                cinder_api.terminate_connection(
                    volume_id, get_volume_connector_properties())

                # Cinder-volume might have completed volume attach. So
                # we should detach the volume. If the attach did not
                # happen, the detach request will be ignored.
                cinder_api.detach(volmap)

        return device_info['path']

    def _connect_volume(self, conn_info):
        protocol = conn_info['driver_volume_type']
        connector = get_volume_connector(protocol)
        device_info = connector.connect_volume(conn_info['data'])
        return device_info

    def _disconnect_volume(self, conn_info):
        protocol = conn_info['driver_volume_type']
        connector = get_volume_connector(protocol)
        connector.disconnect_volume(conn_info['data'], None)

    def detach_volume(self, context, volmap):
        volume_id = volmap.cinder_volume_id
        try:
            self.cinder_api.begin_detaching(volume_id)
        except cinder_exception.BadRequest as e:
            raise exception.Invalid(_("Invalid volume: %s") %
                                    six.text_type(e))

        conn_info = jsonutils.loads(volmap.connection_info)
        if not self._volume_connection_keep(context, volume_id):
            try:
                self._disconnect_volume(conn_info)
            except Exception:
                with excutils.save_and_reraise_exception():
                    LOG.exception('Failed to disconnect volume %(volume_id)s',
                                  {'volume_id': volume_id})
                    self.cinder_api.roll_detaching(volume_id)

            self.cinder_api.terminate_connection(
                volume_id, get_volume_connector_properties())
        self.cinder_api.detach(volmap)

    def _volume_connection_keep(self, context, volume_id):
        host = CONF.host
        db_volumes = objects.VolumeMapping.list_by_cinder_volume(
            context, volume_id)
        volume_hosts = [db_volume.host for db_volume in db_volumes]

        if volume_hosts.count(host) == 1:
            return False
        return True

    def delete_volume(self, volmap):
        volume_id = volmap.cinder_volume_id
        try:
            self.cinder_api.delete_volume(volume_id)
        except cinder_exception as e:
            raise exception.Invalid(_("Delete Volume failed: %s") %
                                    six.text_type(e))
