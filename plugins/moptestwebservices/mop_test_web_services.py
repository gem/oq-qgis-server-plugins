# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# oq-qgis-server-plugins
# Copyright (C) 2019 GEM Foundation
#
# oq-geoviewer is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# oq-geoviewer is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import json
import time
from qgis.server import QgsService, QgsServerProjectUtils


class MOPTEST(QgsService):
    def __init__(self):
        QgsService.__init__(self)

    def name(self):
        return "MOPTEST"

    def version(self):
        return "1.0.0"

    def allowMethod(method):
        return True

    def executeRequest(self, request, response, project):

        time.sleep(10)
        response.setStatusCode(200)
        response.write(
            json.dumps({'owner': 'mop', 'test': True},
                       indent=4, sort_keys=True))


class MOPTEST_REG():

    def __init__(self, serverIface):
        self.serv = MOPTEST()
        serverIface.serviceRegistry().registerService(MOPTEST())
