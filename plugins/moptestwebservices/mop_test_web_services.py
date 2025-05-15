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

import io
import numpy
import json
from qgis.server import QgsService
from qgis.core import (
    QgsProject, QgsVectorFileWriter, QgsVectorLayer,
    QgsPointXY, QgsField, QgsFeature, QgsGeometry,
    edit)  # QgsPoint, QgsFeatureRequest
from qgis.PyQt.QtCore import QVariant, pyqtRemoveInputHook
from requests import Session


def _mute():
    pyqtRemoveInputHook()
# import pdb ; _mute() ; pdb.set_trace()


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
        # Get the project instance
        project = QgsProject.instance()
        # Print the current project file name (might be
        # empty in case no projects have been loaded)
        # print(project.fileName())

        calc_id = request.parameter('CALC_ID')
        imts = request.parameter('IMTS').split(',')

        hostname = 'http://127.0.0.1:8800'
        session = Session()

        # engine_login(hostname, None, None, session)

        # retrieve list of calculations
        resp = session.get(
            'http://127.0.0.1:8800/v1/calc/list', timeout=10, verify=False,
            allow_redirects=False)

        resp = session.get(
            'http://127.0.0.1:8800/v1/calc/%d/extract/oqparam' % (int(calc_id),),
            timeout=100, verify=False, allow_redirects=False)

        js = bytes(numpy.load(io.BytesIO(resp.content))['json'])
        oqparam = json.loads(js)

        if (set(imts) - set([x for x in oqparam['hazard_imtls']])) != set():
            print('FIXME: failure here')

        # Clean current project
        project.clear()

        # Load another project
        project.read(
            '/home/nastasi/git/oq-geoviewer'
            '/project_samples/papers/PapersTmpl.qgs')
        print(project.fileName())
        for imt in imts:
            resp = session.get(
                'http://127.0.0.1:8800/v1/calc/%d/extract/avg_gmf?imt=%s' % (
                    int(calc_id), imt),
                timeout=100, verify=False, allow_redirects=False)

            try:
                if numpy.__version__ >= '1.24.0':
                    extracted_npz = numpy.load(
                        io.BytesIO(resp.content), allow_pickle=False,
                        max_header_size=100000)
                else:
                    extracted_npz = numpy.load(
                        io.BytesIO(resp.content), allow_pickle=False)
            except Exception as exc:
                print('FIXME: failure here')

            # New vector layer initialization
            layer = QgsVectorLayer("Point", imt, "memory")

            # Add fields to the layer
            layer.dataProvider().addAttributes([
                # QgsField("id", QVariant.Int),
                QgsField(imt, QVariant.Double)
            ])
            layer.updateFields()

            extracted_tuples = numpy.column_stack((
                extracted_npz['lons'], extracted_npz['lats'],
                extracted_npz[imt]))

            #  import pdb ; _mute() ; pdb.set_trace()

            all_features = []
            with edit(layer):
                # for idx in range(0, len(extracted_npz['lons'])):
                # for idx in range(0, 100):
                for idx, (lon, lat, imt_val) in enumerate(
                        extracted_tuples):
                    # import pdb ; _mute() ; pdb.set_trace()
                    if (idx % 1000) == 0:
                        print("Idx: %d" % idx)

                    # Create some sample features
                    feature = QgsFeature()
                    feature.setGeometry(QgsGeometry.fromPointXY(
                        QgsPointXY(lon, lat)))
                    feature.setAttributes([imt_val])
                    all_features.append(feature)

                print('Post loop')
                layer.dataProvider().addFeatures(all_features)

                # Update the layer extension
                layer.updateExtents()

            # Save layer as GeoPackage
            save_options = QgsVectorFileWriter.SaveVectorOptions()
            save_options.driverName = "GPKG"
            layer_name = imt
            save_options.layerName = layer_name
            gpkg_filepath = (
                '/home/nastasi/git/oq-geoviewer'
                '/project_samples/papers/out/Papers03_%s.gpkg' % imt)
            error = QgsVectorFileWriter.writeAsVectorFormat(
                layer, gpkg_filepath,
                "UTF-8", layer.crs(), "GPKG", layerOptions=['OVERWRITE=YES'])

            if error[0] == QgsVectorFileWriter.NoError:
                print("Layer save: success")
            else:
                print("Layer save: error:", error)

            imt_layer = QgsVectorLayer(gpkg_filepath, layer_name, 'ogr')

            # add gpkg layer to current QGIS project
            QgsProject.instance().addMapLayer(imt_layer)

        project.write(
            '/home/nastasi/git/oq-geoviewer'
            '/project_samples/papers/out/Papers03.qgz')

        response.setStatusCode(200)
        response.write(
            json.dumps({'owner': 'mop', 'test': project.fileName()},
                       indent=4, sort_keys=True))


class MOPTEST_REG():

    def __init__(self, serverIface):
        self.serv = MOPTEST()
        serverIface.serviceRegistry().registerService(MOPTEST())
