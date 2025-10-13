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
import re
import os
import csv
import math
import numpy
import json
import tempfile
from zipfile import ZipFile
import bisect
from requests import Session

import pprint

from qgis.core import Qgis
from qgis.server import QgsService, QgsServerProjectUtils, QgsServerSettings
from qgis.core import (
    QgsRasterLayer, QgsProject, QgsVectorLayer, QgsVectorFileWriter,
    QgsField, edit, QgsFeature, QgsPointXY, QgsGeometry,
    QgsReferencedRectangle, QgsSymbol, QgsGradientColorRamp,
    QgsGraduatedSymbolRenderer, QgsRuleBasedRenderer, QgsCoordinateTransform,
    QgsApplication, QgsStyle, QgsFillSymbol, NULL,
    QgsWkbTypes, QgsClassificationJenks, QgsClassificationRange,
)

from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtCore import QVariant, QSize, QUrl, QUrlQuery
from qgis.gui import QgsMapCanvas

import processing

from .svir_utils.shared import RAMP_EXTREME_COLORS
from .svir_utils.utils import get_style

# FIXME reenable folder deletion after zip creation
from .gem_common import gem_log, rmdir_recursive, alphanum_rndstr
from .lock import acquire_lock, release_lock
from .zipdir import zipdir

import xml.etree.ElementTree as ET

if Qgis.QGIS_VERSION_INT < 31000:
    # the following is an enum
    DEFAULT_STYLE_MODE = QgsGraduatedSymbolRenderer.Quantile
else:
    # Quantile is the id of QgsClassificationQuantile
    DEFAULT_STYLE_MODE = 'Quantile'


DEFAULT_SETTINGS = dict(
    color_from_rgba=QColor('#FFEBEB').rgba(),
    color_to_rgba=QColor('red').rgba(),
    style_mode=DEFAULT_STYLE_MODE,
    style_classes=10,
    force_restyling=True,
    experimental_enabled=False,
    developer_mode=False,
    log_level='C',
)

#
# NOTE: _get_material and _get_occupancy will be added as part of
#       the oq-gem-taxonomy python library in the new query swiss-knife function
#
def _get_material(taxonomy):
    mat = re.findall('^[A-Z0-9]+', taxonomy)
    if len(mat) < 1:
        return 'UNK'
    return mat[0]

def _get_occupancy(taxonomy):
    occ = re.findall('[^A-Z0-9]?(RES|COM|MIX|IND|AGR|GOV|EDU'
                     '|HEA|TRT|OCO)[^A-Z[0-9]?', taxonomy)
    if len(occ) < 1:
        return 'UNK'
    return occ[0]

def _create_layer_from_selected(source_layer, with_attributes=False):
    """Create a new layer containing only selected features"""
    if source_layer.selectedFeatureCount() == 0:
        print("No features selected!")
        return None

    # Create memory layer with same CRS and geometry type
    geom_type = QgsWkbTypes.displayString(source_layer.wkbType())
    crs = source_layer.crs().authid()
    temp_layer = QgsVectorLayer(f'{geom_type}?crs={crs}', 'selected_features', 'memory')

    temp_provider = temp_layer.dataProvider()
    temp_provider.createSpatialIndex()
    if with_attributes:
        temp_layer.startEditing()
        temp_provider.addAttributes(source_layer.fields())
        temp_layer.updateFields()
        temp_layer.commitChanges()

    # Copy selected features
    selected_features = list(source_layer.getSelectedFeatures())
    temp_provider.addFeatures(selected_features)
    temp_layer.updateExtents()

    return temp_layer

def _style_curves(layer, style_by):
    use_sgc_style = False
    opacity = 1.0

    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    symbol.setOpacity(opacity)

    style = get_style(layer, None)

    ramp = QgsGradientColorRamp(
        style['color_from'], style['color_to'])

    style_mode = style['style_mode']

    default_qgs_style = QgsStyle().defaultStyle()
    default_color_ramp_names = default_qgs_style.colorRampNames()

    style_mode = 'EqualInterval'

    ramp_type_idx = default_color_ramp_names.index('Spectral')
    inverted = True
    symbol.setColor(QColor(RAMP_EXTREME_COLORS['Reds']['top']))

    ramp = default_qgs_style.colorRamp(
        default_color_ramp_names[ramp_type_idx])

    symbol.setColor(QColor(RAMP_EXTREME_COLORS['Reds']['top']))

    if inverted:
        ramp.invert()

    # get unique values
    fni = layer.fields().indexOf(style_by)
    unique_values = layer.dataProvider().uniqueValues(fni)
    num_unique_values = len(unique_values - {NULL})
    gem_log('num_uniq: %d, QGIS vers: %d' % (
        num_unique_values, Qgis.QGIS_VERSION_INT), Qgis.Critical)

    if num_unique_values > 2:
        if Qgis.QGIS_VERSION_INT < 31000:
            renderer = QgsGraduatedSymbolRenderer.createRenderer(
                layer,
                style_by,
                min(num_unique_values, style['classes']),
                style_mode,
                symbol.clone(),
                ramp)
        else:
            renderer = QgsGraduatedSymbolRenderer(
                style_by, [])
            # NOTE: the following returns an instance of one of the
            #       subclasses of QgsClassificationMethod
            classification_method = \
                QgsApplication.classificationMethodRegistry().method(
                    style_mode)
            renderer.setClassificationMethod(classification_method)
            renderer.updateColorRamp(ramp)
            renderer.updateSymbols(symbol.clone())
            renderer.updateClasses(
                layer, min(num_unique_values, style['classes']))
            print('post updateClasses')
        if not use_sgc_style:
            if Qgis.QGIS_VERSION_INT < 31000:
                label_format = renderer.labelFormat()
                # NOTE: the following line might be useful
                # label_format.setTrimTrailingZeroes(True)
                label_format.setPrecision(2)
                renderer.setLabelFormat(label_format, updateRanges=True)
            else:
                print('not use_sgc_style')
                renderer.classificationMethod().setLabelPrecision(2)
                renderer.calculateLabelPrecision()

    layer.setRenderer(renderer)
    layer.setOpacity(opacity)


class EWMS(QgsService):

    def __init__(self):
        QgsService.__init__(self)

    def name(self):
        return "EWMS"

    def version(self):
        return "1.0.0"

    def allowMethod(method):
        return True

    def executeRequest(self, request, response, project):
        if request.parameters()['REQUEST'] == 'GetLayerNames':
            try:
                self._get_layer_names(
                    request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'GetLayersCustomProperties':
            try:
                self._get_custom_properties_by_layers(
                    request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'GetProjectCustomProperties':
            try:
                self._get_custom_properties_by_project(
                    request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'GetLayerFields':
            try:
                self._get_fields_by_layer(request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'GetLayerStyles':
            try:
                self._get_styles_by_layer(request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
        elif request.parameters()['REQUEST'] == 'OQEngine2Map':
            try:
                self._oq_engine_calc2map(request, response, project)
            except Exception as exc:
                response.setStatusCode(500)
                response.write("An error occurred: %s" % exc)
                response.close()
        else:
            response.setStatusCode(400)
            response.write("Missing or invalid 'REQUEST' parameter")

    def _get_layer_names(self, request, response, project):
        layer_names = [
            layer.name() for id, layer in project.mapLayers().items()]
        response.setStatusCode(200)
        response.write(
            json.dumps(layer_names, indent=4, sort_keys=True))

    def _get_custom_properties_by_layers(self, request, response, project):
        if QgsServerProjectUtils.wmsUseLayerIds(project):
            dict_key = 'id'
        else:
            dict_key = 'name'
        try:
            layer_keys_str = request.parameters()['LAYERS']
        except KeyError:
            layer_keys_str = None
        try:
            filter_str = request.parameters()['FILTER']
        except KeyError:
            filter_str = None
        if layer_keys_str:
            layer_keys = layer_keys_str.split(',')
        else:
            layer_keys = None
        if filter_str:
            filter_prop_items = [filter_prop.split(':')
                                 for filter_prop in filter_str.split(',')]
            filter_props = {
                filter_prop_name: filter_prop_value
                for filter_prop_name, filter_prop_value in filter_prop_items}
        else:
            filter_props = None
        custom_props = {}
        for layer_id, layer in project.mapLayers().items():
            # If a shortname is set, we must use it instead of
            # the plain layer name
            layer_name = layer.serverProperties().shortName() or layer.name()
            if dict_key == 'name':
                custom_props_key = layer_name
            else:
                custom_props_key = layer_id
            if layer_keys:
                if dict_key == 'name' and layer_name not in layer_keys:
                    continue
                if dict_key == 'id' and layer_id not in layer_keys:
                    continue
            custom_props[custom_props_key] = {}
            if dict_key == 'name':
                custom_props[layer_name]['layer_id'] = layer_id
            else:
                custom_props[layer_id]['layer_name'] = layer_name
            for prop in layer.customPropertyKeys():
                prop_value = layer.customProperty(prop)
                custom_props[custom_props_key][prop] = prop_value
        custom_props_filtered = custom_props.copy()
        if filter_props:
            for filter_prop in filter_props:
                for layer in custom_props:
                    filter_prop_value = filter_props[filter_prop]
                    if custom_props[layer][filter_prop] != filter_prop_value:
                        del custom_props_filtered[layer]
        response.setStatusCode(200)
        response.write(
            json.dumps(custom_props_filtered, indent=4, sort_keys=True))

    def _get_custom_properties_by_project(self, request, response, project):
        response.setStatusCode(200)
        response.write(
            json.dumps(project.customVariables(), indent=4, sort_keys=True))

    def _get_fields_by_layer(self, request, response, project):
        if QgsServerProjectUtils.wmsUseLayerIds(project):
            dict_key = 'id'
        else:
            dict_key = 'name'
        try:
            layer_keys_str = request.parameters()['LAYERS']
        except KeyError:
            layer_keys_str = None
        if layer_keys_str:
            layer_keys = layer_keys_str.split(',')
        else:
            layer_keys = None
        fields_by_layer = {}
        for layer_id, layer in project.mapLayers().items():
            if isinstance(layer, QgsRasterLayer):
                continue
            # If a shortname is set, we must use it instead of
            # the plain layer name
            layer_name = layer.serverProperties().shortName() or layer.name()
            if dict_key == 'name':
                layer_key = layer_name
            else:
                layer_key = layer_id
            if layer_keys:
                if dict_key == 'name' and layer_name not in layer_keys:
                    continue
                if dict_key == 'id' and layer_id not in layer_keys:
                    continue
            fields_by_layer[layer_key] = {}
            if dict_key == 'name':
                fields_by_layer[layer_name]['layer_id'] = layer_id
            else:
                fields_by_layer[layer_id]['layer_name'] = layer_name
            fields_by_layer[layer_key] = [
                field.name() for field in layer.fields()]
        response.setStatusCode(200)
        response.write(
            json.dumps(fields_by_layer, indent=4, sort_keys=True))

    def _get_styles_by_layer(self, request, response, project):
        if QgsServerProjectUtils.wmsUseLayerIds(project):
            dict_key = 'id'
        else:
            dict_key = 'name'
        try:
            layer_keys_str = request.parameters()['LAYERS']
        except KeyError:
            layer_keys_str = None
        if layer_keys_str:
            layer_keys = layer_keys_str.split(',')
        else:
            layer_keys = None
        styles_by_layer = {}
        for layer_id, layer in project.mapLayers().items():
            if isinstance(layer, QgsRasterLayer):
                continue
            # If a shortname is set, we must use it instead of
            # the plain layer name
            layer_name = layer.serverProperties().shortName() or layer.name()
            if dict_key == 'name':
                layer_key = layer_name
            else:
                layer_key = layer_id
            if layer_keys:
                if dict_key == 'name' and layer_name not in layer_keys:
                    continue
                if dict_key == 'id' and layer_id not in layer_keys:
                    continue
            styles_by_layer[layer_key] = {}
            if dict_key == 'name':
                styles_by_layer[layer_name]['layer_id'] = layer_id
            else:
                styles_by_layer[layer_id]['layer_name'] = layer_name
            styles_by_layer[layer_key] = layer.styleManager().styles()
        response.setStatusCode(200)
        response.write(
            json.dumps(styles_by_layer, indent=4, sort_keys=True))

    def _oq_engine_calc2map(self, request, response, project):
        # calculation_mode: 'scenario' or 'scenario_damage'
        calculation_mode = request.parameter('CALCULATION_MODE')
        method_name = '_oq_engine_calc2map_%s' % calculation_mode
        if not hasattr(self, method_name):
            response.setStatusCode(400)
            response.write(
                json.dumps({'status': 'fail',
                            'reason': 'no rules to produce maps for calculation_mode "%s".' %
                            calculation_mode}, indent=4, sort_keys=True))
            return

        eng_proto = 'http'
        eng_name = 'host.docker.internal'
        eng_port = '8800'
        engine_url = '%s://%s:%s' % (eng_proto, eng_name, eng_port)

        method = getattr(self, method_name)

        return method(request, response, project, engine_url)

    def _oq_engine_calc2map_scenario(self, request, response, project, engine_url):
        os.umask(0o0002)
        # Get the project instance
        project = QgsProject.instance()
        # Print the current project file name (might be
        # empty in case no projects have been loaded)
        # print(project.fileName())

        # create random prefix to avoid clash names

        gem_log('calc2map', Qgis.Critical)
        calc_id = request.parameter('CALC_ID')
        description = request.parameter('DESCRIPTION')
        imts = request.parameter('IMTS').split(',')

        session = Session()

        # engine_login(hostname, None, None, session)

        gem_log('calc2map: pre get', Qgis.Critical)
        oqparam_req = '%s/v1/calc/%d/extract/oqparam' % (
            engine_url, int(calc_id),)
        gem_log('calc2map: pre oq-param: [%s]' % oqparam_req, Qgis.Critical)
        resp = session.get(
            oqparam_req, timeout=100, verify=False, allow_redirects=False)

        gem_log('calc2map: post oq-param', Qgis.Critical)
        js = bytes(numpy.load(io.BytesIO(resp.content))['json'])
        calc = json.loads(js)

        calc_imtls = (calc['risk_imtls'] if 'risk_imtls' in calc
                      else calc['hazard_imtls'])
        if (set(imts) - set([x for x in calc_imtls])) != set():
            gem_log("calc2map: list of imt doesn't match calc imts",
                    Qgis.Critical)
            response.setStatusCode(500)
            response.write("calc2map: list of imt doesn't match calc imts")
            return

        # Clean current project
        project.clear()

        for i in range(0, 5):
            rnd_sfx = alphanum_rndstr(8)
            project_name = '%s_%s' % (description, rnd_sfx)
            lock_filename = '/io/uploads/projects/%s.lock' % project_name
            if acquire_lock(lock_filename):
                break
        else:
            gem_log('calc2map: lock of %s failed' % lock_filename,
                    Qgis.Critical)
            response.setStatusCode(500)
            response.write('calc2map: lock of %s failed' % lock_filename)
            return

            gem_log('calc2map: lock acquired %s' % lock_filename,
                    Qgis.Critical)

        # this 'try:' is to be able to unlock the locked file at the end of
        # project creation procedure
        try:
            # Load another project
            project.read('/io/data/_templates/Papers/PapersTmpl.qgs')
            gem_log('calc2map: project name: %s' % project.fileName(),
                    Qgis.Critical)

            # mkdir of base for all files of the project
            project_folder = '/io/uploads/projects/%s' % project_name
            layer_folder = '%s/layers' % project_folder
            os.mkdir(project_folder)
            os.mkdir(layer_folder)
            for imt in imts[::-1]:
                resp = session.get(
                    '%s/v1/calc/%d/extract/avg_gmf?imt=%s' % (
                        engine_url, int(calc_id), imt),
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
                    print('FIXME: failure here %s' % exc)

                # New vector layer initialization
                layer = QgsVectorLayer("Point", imt, "memory")

                # Add fields to the layer
                layer.dataProvider().addAttributes([
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
                        # if (idx % 1000) == 0:
                        #     print("Idx: %d, lon %f lat %f val %f" % (
                        #           idx, lon, lat, imt_val))

                        # if idx == 1000:
                        #     break

                        # Create some sample features
                        feature = QgsFeature()
                        feature.setGeometry(QgsGeometry.fromPointXY(
                            QgsPointXY(float(lon), float(lat))))
                        feature.setAttributes([float(imt_val)])
                        all_features.append(feature)

                    layer.dataProvider().addFeatures(all_features)

                    # Update the layer extension
                    layer.updateExtents()

                # Save layer as GeoPackage
                save_options = QgsVectorFileWriter.SaveVectorOptions()
                save_options.driverName = "GPKG"
                layer_name = imt
                save_options.layerName = layer_name

                gpkg_filepath = '%s/%s_%s_%s.gpkg' % (
                    layer_folder, description, imt, rnd_sfx)
                gem_log('calc2map: pre layer save [%s]' % gpkg_filepath,
                        Qgis.Critical)
                error = QgsVectorFileWriter.writeAsVectorFormat(
                    layer, gpkg_filepath,
                    "UTF-8", layer.crs(), "GPKG",
                    layerOptions=['OVERWRITE=YES'])

                gem_log('calc2map: post layer save', Qgis.Critical)

                if error[0] == QgsVectorFileWriter.NoError:
                    print("Layer save: success")
                else:
                    print("Layer save: error:", error)

                imt_layer = QgsVectorLayer(gpkg_filepath, layer_name, 'ogr')

                gem_log('IMT: %s' % imt, Qgis.Critical)

                _style_curves(imt_layer, imt)

                # add gpkg layer to current QGIS project
                project.addMapLayer(imt_layer)

                extent = layer.extent()
                ref_rect = QgsReferencedRectangle(extent, layer.crs())
                vs_project = project.viewSettings()
                vs_project.setDefaultViewExtent(ref_rect)

            gem_log('calc2map: pre project save', Qgis.Critical)

            # Create canvas
            canvas = QgsMapCanvas()
            canvas.setObjectName("theMapCanvas")
            # Set canvas size
            canvas.resize(QSize(800, 600))

            # FIXME: set proper values

            # Set coordinate reference system
            # crs = QgsCoordinateReferenceSystem("EPSG:4326")
            canvas.setDestinationCrs(project.crs())

            # Set extent
            # extent = ref_rect  # QgsRectangle(-180, -90, 180, 90)

            layer_extent = extent
            source_crs = layer.crs()
            dest_crs = project.crs()
            transform = QgsCoordinateTransform(source_crs, dest_crs, project)
            transformed_extent = transform.transformBoundingBox(layer_extent)
            canvas.setExtent(transformed_extent)

            #
            #  save qgis project
            #
            project_filepath = '%s/%s.qgs' % (project_folder, project_name)
            project.write(project_filepath)
            project_filename = project.fileName()
            project.clear()
            gem_log('calc2map: post project save, filename [%s]' %
                    project_filename, Qgis.Critical)

            old_dir = os.getcwd()
            os.chdir(project_folder)

            gem_log('calc2map: chdir("%s")' % project_folder, Qgis.Critical)

            archive_pathname = '%s.zip' % project_folder
            zipdir(archive_pathname, '.')
            os.chdir(old_dir)

            if os.getenv('GEM_GV_KEEP_CALC_PROJECT', False) is False:
                rmdir_recursive(project_folder)

            gem_log('calc2map: post project zip', Qgis.Critical)

            response.setStatusCode(200)
            response.write(
                json.dumps({'owner': 'mop',
                            'uploaded_file': os.path.basename(
                                archive_pathname)},
                           indent=4, sort_keys=True))
        finally:
            release_lock(lock_filename)

    def _oq_engine_calc2map_scenario_damage(self, request, response, project, engine_url):
        meanqua_sfx = ['mean', 'qt05', 'qt95']
        qta_init = {
            'lay': None,
            'dp': None,
            'descr': None,
            'ramp_col': None,
            }

        for sfx in meanqua_sfx:
            qta_init['tot_%s' % sfx] = None
            qta_init['div_%s' % sfx] = None
            qta_init['maggr_%s' % sfx] = None

        quantities = {}

        quantities['fatalities'] = qta_init.copy()
        quantities['fatalities']['descr'] = 'Fatalities'
        quantities['fatalities']['ramp_col'] = 'Greens'
        quantities['fatalities']['field'] = 'structural-fatalities'
        quantities['fatalities']['rel_fields'] = ['value-residents']

        quantities['economic_losses'] = qta_init.copy()
        quantities['economic_losses']['descr'] = 'Economic Losses (USD)'
        quantities['economic_losses']['ramp_col'] =  'Reds'
        quantities['economic_losses']['field'] = 'structural-losses'
        quantities['economic_losses']['rel_fields'] = ['value-structural','value-nonstructural','value-contents']

        quantities['complete_damage'] = qta_init.copy()
        quantities['complete_damage']['descr'] = 'Buildings Beyond Repair'
        quantities['complete_damage']['ramp_col'] = 'Blues'
        quantities['complete_damage']['field'] = 'structural-complete'
        quantities['complete_damage']['rel_fields'] = ['value-number']

        # to speedup devel set it to a small value (100 is a good value)
        MAX_FEATURES =  os.getenv('GEM_GV_MAX_FEATURES', -1)

        # number of classified classes
        CLASSIFIED_CLASSES = 7

        aggrs_by = {
            'material': {
                'get': _get_material
            },
            'occupancy': {
                'get': _get_occupancy
            }
        }

        # Get the project instance
        project = QgsProject.instance()

        gem_log('calc2map_scenario_damage:BEGIN', Qgis.Info)
        calc_id = request.parameter('CALC_ID')
        description = request.parameter('DESCRIPTION')
        adm_level = request.parameter('ADM_LEVEL')
        session = Session()

        oqparam_req = '%s/v1/calc/%d/results' % (
            engine_url, int(calc_id),)
        gem_log('calc2map: pre oq-param: [%s]' % oqparam_req, Qgis.Info)
        print('OQDOWNLOAD: Calc_Result: %s' % oqparam_req)
        resp = session.get(
            oqparam_req, timeout=100, verify=False, allow_redirects=False)

        gem_log('calc2map: post oq-param', Qgis.Info)
        damages_stats_entries = [x for x in json.load(io.BytesIO(resp.content)) if
                              x['type'] == 'damages-stats']
        if len(damages_stats_entries) != 1:
            response.setStatusCode(500)
            response.write("calc2map: 'damages-stats' output not found")
            return

        # retrieve damages-stats output
        print('OQDOWNLOAD: Damages_stats: %s' % damages_stats_entries[0]['url'])
        damages_stats = session.get("%s" % damages_stats_entries[0]['url'],
                                    timeout=600, verify=False, allow_redirects=False)

        # extract and populate exposure dictionary
        exposure_entries = [x for x in json.load(io.BytesIO(resp.content))
                            if x['type'] == 'exposure']
        if len(exposure_entries) != 1:
            response.setStatusCode(500)
            response.write("calc2map: 'exposure' output not found")
            return

        # retrieve damages-stats output
        print('OQDOWNLOAD: Exposure_data: %s' % exposure_entries[0]['url'])
        exposure_data = session.get("%s" % exposure_entries[0]['url'],
                               timeout=600, verify=False, allow_redirects=False)

        exposure = {}
        with tempfile.TemporaryDirectory() as tempdir:
            arch_filename = os.path.join(tempdir, 'exposure_data.zip')
            with open(arch_filename, "wb") as fp_out:
                fp_out.write(exposure_data.content)
            with ZipFile(arch_filename, "r") as zip_archive:
                zip_archive.extractall(tempdir)

            xml_files = [f for f in os.listdir(tempdir) if f.endswith('.xml')]

            if len(xml_files) != 1:
                gem_log('calc2map: more than one xml file in exposure [%s]' %
                        xml_files, Qgis.Critical)
                response.setStatusCode(500)
                response.write('calc2map: more than one xml file in exposure [%s]' %
                               xml_files)
                return

            tree = ET.parse(os.path.join(tempdir, xml_files[0]))
            ns = {'n': 'http://openquake.org/xmlns/nrml/0.5'}
            assets_tags = tree.findall("./n:exposureModel/n:assets", namespaces=ns)
            if len(assets_tags) != 1:
                gem_log('calc2map: multiple assets tags not supported [%s]' %
                        assets_tags, Qgis.Critical)
                response.setStatusCode(500)
                response.write('calc2map: multiple assets tags not supported [%s]' %
                               assets_tags)
                return

            csv_filenames = [x.strip() for x in assets_tags[0].text.split('\n')
                             if x.strip() != '']

            if len(csv_filenames) != 1:
                gem_log('calc2map: multiple assets elements not supported [%s]' %
                        csv_filenames, Qgis.Critical)
                response.setStatusCode(500)
                response.write(
                    'calc2map: multiple assets elements not supported [%s]' %
                    csv_filenames)
                return

            with open(os.path.join(tempdir, csv_filenames[0]), 'r') as exposure_csv_fp:
                exposure_csv = csv.DictReader(exposure_csv_fp)
                for exposure_row in exposure_csv:
                    exp_id = exposure_row['id']
                    exposure[exp_id] = {
                        'id': exposure_row['id'],
                        'lon': exposure_row['lon'],
                        'lat': exposure_row['lat'],
                        }
                    for qta_key, qta in quantities.items():
                        exposure[exp_id][qta_key] = sum(
                            [float(exposure_row[rel_field]) for
                             rel_field in qta['rel_fields']
                             ])

        # Clean current project
        project.clear()

        # create random prefix to avoid clash names
        for i in range(0, 5):
            rnd_sfx = alphanum_rndstr(8)
            project_name = '%s_%s' % (description, rnd_sfx)
            lock_filename = '/io/uploads/projects/%s.lock' % project_name
            if acquire_lock(lock_filename):
                break
        else:
            gem_log('calc2map: lock of %s failed' % lock_filename,
                    Qgis.Critical)
            response.setStatusCode(500)
            response.write('calc2map: lock of %s failed' % lock_filename)
            return

        gem_log('calc2map: lock acquired %s' % lock_filename,
                Qgis.Info)

        # this 'try...finally' is to be able to unlock the locked file
        # at the end of project creation procedure and remove temporary csv file
        try:
            headers = damages_stats.headers
            content_type = headers.get('content-type').split(';')[0]
            if content_type.upper() != 'APPLICATION/X-ZIP':
                response.setStatusCode(400)
                response.setHeader('content-type',
                                   'application/json; charset=utf-8')
                response.write(
                    json.dumps({
                        'status': 'fail',
                        'reason': (
                            "for 'damages_stats' a zip file was expected,"
                            " instead a '%s' is retrieved, quantiles are"
                            " required as output for this calculation?" %
                            content_type)
                    }, indent=4, sort_keys=True))

                return

            # --- OLD IMPLEMENTATION: BEGIN ---
            # fp_out = tempfile.NamedTemporaryFile(mode="w", delete=False)
            # fp_in = io.StringIO(damages_stats.text)
            # # skip info header
            # next(fp_in)
            # csv_in = csv.DictReader(fp_in)
            # fieldnames = ['asset_id', 'lon', 'lat', 'taxonomy']

            # for qta_key, qta in quantities.items():
            #     fieldnames += [qta['field']]

            # csv_out = csv.DictWriter(fp_out, fieldnames)
            # csv_out.writeheader()
            # for row_in in csv_in:
            #     csv_out.writerow({name: row_in[name] for name in fieldnames})
            # fp_out.close()

            # --- OLD IMPLEMENTATION: FINISH ---

            with tempfile.TemporaryDirectory() as tempdir:
                arch_filename = os.path.join(tempdir, 'damage_stats.zip')
                with open(arch_filename, "wb") as fp_out:
                    fp_out.write(damages_stats.content)
                with ZipFile(arch_filename, "r") as zip_archive:
                    zip_archive.extractall(tempdir)

                print('LISTDIR: %s' % os.listdir(tempdir))
                print('calc_id: %d' % int(calc_id))
                fp_out = tempfile.NamedTemporaryFile(mode="w", delete=False)
                print('CSVOUT avg_damages: %s' % fp_out.name)
                meanqua_fname = ['avg_damages-mean_%d.csv',
                                 'avg_damages-quantile-0.05_%d.csv',
                                 'avg_damages-quantile-0.95_%d.csv']

                fp_in = {}
                csv_in = {}
                fieldnames_comm = ['asset_id', 'lon', 'lat', 'taxonomy']
                fieldnames = fieldnames_comm[:]
                for sfx, fname in list(zip(meanqua_sfx, meanqua_fname)):
                    fp_in[sfx] = open(os.path.join(tempdir, fname % int(calc_id)))
                    # skip info header
                    next(fp_in[sfx])

                    csv_in[sfx] = csv.DictReader(fp_in[sfx])
                    for qta_key, qta in quantities.items():
                        fieldnames += ["%s_%s" % (qta['field'], sfx)]

                csv_out = csv.DictWriter(fp_out, fieldnames)
                csv_out.writeheader()
                row = {}

                for row['mean'], row['qt05'], row['qt95'] in zip(
                        csv_in['mean'], csv_in['qt05'], csv_in['qt95']):
                    csv_row = {name: row['mean'][name] for name in fieldnames_comm}

                    for sfx in meanqua_sfx:
                        for qta_key, qta in quantities.items():
                            k = "%s_%s" % (qta['field'], sfx)
                            v = row[sfx][qta['field']]
                            csv_row[k] = v
                    csv_out.writerow(csv_row)
                fp_out.close()

            # create VectorLayer from filtered CSV file
            lines_to_skip_count = 0
            url = QUrl.fromLocalFile(fp_out.name)
            # url = QUrl("%s" % calc[0]['url'])
            url_query = QUrlQuery()
            url_query.addQueryItem('type', 'csv')
            url_query.addQueryItem('xField', 'lon')
            url_query.addQueryItem('yField', 'lat')
            url_query.addQueryItem('spatialIndex', 'yes')
            # url_query.addQueryItem('crs', 'epsg:4326')
            url_query.addQueryItem('subsetIndex', 'no')
            url_query.addQueryItem('watchFile', 'no')
            url_query.addQueryItem('delimiter', ',')
            url_query.addQueryItem('quote', '"')
            url_query.addQueryItem('skipLines', str(lines_to_skip_count))
            url_query.addQueryItem('trimFields', 'yes')
            url_query.addQueryItem('export_type', 'csv')
            url.setQuery(url_query)
            layer_uri = url.toString()
            gem_log('calc2map: layer uri: [%s]' % layer_uri, Qgis.Info)
            points_layer_csv = QgsVectorLayer(layer_uri, 'points_layer', 'delimitedtext')

            # create a in-memory layer copy for performance reason
            points_layer_csv.selectAll()
            points_layer = _create_layer_from_selected(points_layer_csv, with_attributes=True)
            points_layer_csv.removeSelection()

            print('points_layer creation: finish')

            if not points_layer.isValid():
                response.setStatusCode(500)
                response.write("calc2map: 'Unable to copy points_layer'")
                return

            # Load project template
            project.read('/io/data/_templates/Papers/PapersTmpl.qgs')
            gem_log('calc2map: project name: %s' % project.fileName(),
                    Qgis.Info)

            # mkdir of base for all files of the project
            project_folder = '/io/uploads/projects/%s' % project_name
            layer_folder = '%s/layers' % project_folder
            os.makedirs(project_folder)
            os.makedirs(layer_folder)

            processing.Processing.initialize()

            # INFO: here to change adm level if required
            # NAME_0 nation, NAME_1 region
            zonal_uri = ('/io/uploads/subdivision_areas/italy_adm%s.gpkg' % adm_level)
            zonal_layer = QgsVectorLayer(zonal_uri, 'italy_adm%s' % adm_level, 'ogr')


            proj_info = {}

            for qta_key, qta in quantities.items():
                proj_info[qta_key] = {'rank_abs': [],
                                      'rank_rel': []}
                for aggr_key in aggrs_by:
                    proj_info[qta_key][aggr_key] = {}
                    for sfx in meanqua_sfx:
                        print('YYYYY: qta_key %s, aggr_key: %s, tot_%s' % (qta_key, aggr_key, sfx))
                        proj_info[qta_key][aggr_key]['tot_%s' % sfx] = {}
                qta['lay'] = QgsVectorLayer(
                    'Polygon?crs=epsg:4326', qta['descr'], 'memory')

                qta['dp'] = qta['lay'].dataProvider()
                qta['lay'].startEditing()

                # Adm 0 not included ID_0 = 'ITA', NAME_0 = 'Italy'
                for depth in range(1, int(adm_level) + 1):
                    qta['lay'].addAttribute(QgsField(
                        'ID_%d' % depth, QVariant.Int))
                    qta['lay'].addAttribute(QgsField(
                        'NAME_%d' % depth, QVariant.String))

                qta['lay'].addAttribute(QgsField('value', QVariant.Double))
                qta['lay'].addAttribute(QgsField('json_info', QVariant.String))
                qta['lay'].commitChanges()

            # sequence to avoid usage of sites multiple times when on regions border
            grouped_sites = set()

            server_settings = QgsServerSettings()
            # loop on group layer and, for each feature select layer points features and
            # process them
            for zonal_feat in zonal_layer.getFeatures():
                points_layer.removeSelection()
                zonal_layer.selectByIds([zonal_feat.id()])

                temp_layer = _create_layer_from_selected(zonal_layer)

                # select sites intersect with single feature temp layer
                processing.run("native:selectbylocation", {
                    'INPUT': points_layer,
                    'PREDICATE': [0],  # 0 = intersects
                    'INTERSECT': temp_layer,
                    'METHOD': 0,
                    'SELECTED_FEATURES_ONLY': True,
                })

                # Remove temporary layer
                if temp_layer and temp_layer.id() in QgsProject.instance().mapLayers():
                    QgsProject.instance().removeMapLayer(temp_layer.id())

                # Or simply delete the layer object
                if temp_layer:
                    del temp_layer

                # if 'OUTPUT' in result:
                if points_layer.selectedFeatureIds():
                    if server_settings.logLevel() <= Qgis.Info:
                        gem_log('N FEATS: %d' % len(
                            points_layer.selectedFeatureIds()),
                                Qgis.Info)

                    for qta_key, qta in quantities.items():
                        qta['div'] = 0.0
                        for sfx in meanqua_sfx:
                            qta['tot_%s' % sfx] = 0.0
                            qta['maggr_%s' % sfx] = {}
                            for aggr_key in aggrs_by:
                                qta['maggr_%s' % sfx][aggr_key] = {}

                    for feat_idx, feat in enumerate(points_layer.selectedFeatures()):
                        if MAX_FEATURES != -1 and feat_idx == MAX_FEATURES:
                            break

                        if feat.id() in grouped_sites:
                            continue
                        else:
                            grouped_sites.add(feat.id())

                            # gem_log('calc2map: feat.id type: %s' % type(feat.id()), Qgis.Critical)

                        feat_exposure = exposure[feat['asset_id']]

                        aggregate_by = {}
                        for aggr_key, aggr in aggrs_by.items():
                            aggregate_by[aggr_key] = aggr['get'](feat['taxonomy'])

                        for qta_key, qta in quantities.items():
                            for sfx in meanqua_sfx:
                                qta['tot_%s' % sfx] += feat["%s_%s" % (qta['field'], sfx)]
                            qta['div'] += feat_exposure[qta_key]

                        for aggr_key, item_key in aggregate_by.items():
                            for qta_key, qta in quantities.items():
                                for sfx in meanqua_sfx:
                                    if item_key in qta['maggr_%s' % sfx][aggr_key]:
                                        qta['maggr_%s' % sfx][aggr_key][item_key] += feat[
                                            "%s_%s" % (qta['field'], sfx)]
                                    else:
                                        qta['maggr_%s' % sfx][aggr_key][item_key] = feat[
                                            "%s_%s" % (qta['field'], sfx)]

                    for qta_key, qta in quantities.items():
                        for sfx in meanqua_sfx:
                            quantity_maggr = qta['maggr_%s' % sfx]
                            for aggr_key in quantity_maggr:
                                for item_key, item_val in quantity_maggr[aggr_key].items():
                                    print('XXXXX: qta_key %s, aggr_key: %s, tot_%s' % (qta_key, aggr_key, sfx))
                                    pprint.pprint(proj_info)
                                    if item_key not in proj_info[qta_key][aggr_key]['tot_%s' % sfx]:
                                        proj_info[qta_key][aggr_key]['tot_%s' % sfx][item_key] = item_val
                                    else:
                                        proj_info[qta_key][aggr_key]['tot_%s' % sfx][item_key] += item_val

                        rank_names = []
                        for depth in range(1, int(adm_level) + 1):
                            rank_names.append(zonal_feat['NAME_%d' % depth])

                        proj_info[qta_key]['rank_abs'].append({'id':zonal_feat.id(),
                                                               'names': rank_names, 'value': qta['tot_mean']})
                        proj_info[qta_key]['rank_rel'].append({'id':zonal_feat.id(),
                                                               'names': rank_names, 'value': qta['tot_mean'] / qta['div']})

                        for rank_key in ['rank_abs', 'rank_rel']:
                            # sort ranked zones
                            new_rank = sorted(proj_info[qta_key][rank_key], key=lambda d: d['value'], reverse=True)
                            proj_info[qta_key][rank_key] = new_rank
                            # riduce rank to 10 elements
                            proj_info[qta_key][rank_key] = proj_info[qta_key][rank_key][:10]

                    # print(f"cdam: {complete_damage_sum}, ecloss: {economic_losses_sum},"
                    #       f" fatal: {fatalities_sum}")

                    for qta_key, qta in quantities.items():
                        if qta['tot_mean'] == 0.0:
                            continue

                        with edit(qta['lay']):
                            feat_out = QgsFeature(qta['lay'].fields())
                            feat_out.setGeometry(zonal_feat.geometry())
                            attrs = []
                            for depth in range(1, int(adm_level) + 1):
                                attrs += [zonal_feat['ID_%d' % depth],
                                          zonal_feat['NAME_%d' % depth]]
                            attrs += [json.dumps(qta['tot_mean']), json.dumps(qta['maggr_mean'])]
                            feat_out.setAttributes(attrs)
                            qta['dp'].addFeatures([feat_out])
                else:
                    print('N FEATS: ZERO')
                zonal_layer.removeSelection()

            pprint.pprint(proj_info)

            print('Analize data to create custom symbology < 1.0 + N groups with same numerosity')
            #
            #  Analize data to create custom symbology < 1.0 + N groups with same numerosity
            #
            lays_values = {}
            lays_classes = {}
            for qta_key, qta in quantities.items():

                lays_values[qta_key] = []
                qta['lay'].selectAll()
                lay_values = lays_values[qta_key]
                attr_idx = -1
                # populate an ordered list of values
                for feat in qta['lay'].selectedFeatures():
                    if attr_idx < 0:
                        attr_idx = feat.fieldNameIndex('value')
                    bisect.insort(lay_values, float(feat.attributes()[attr_idx]))

                # identify the first value greater than one
                lt_one_idx = -1
                for idx, value in enumerate(lay_values):
                    if value > 1.0:
                        break
                    lt_one_idx = idx

                classified_classes_n = CLASSIFIED_CLASSES
                if lt_one_idx == -1:
                    # no lt_one case
                    classified_values = iter(lay_values)
                else:
                    # some elements has less than one value

                    classified_values = iter(lay_values[lt_one_idx:])
                    classified_classes_n -= 1

                cl_jenks = QgsClassificationJenks()
                cla = cl_jenks.classes(classified_values, classified_classes_n)
                lay_classes_float = (
                     cla if lt_one_idx == -1 else
                     [QgsClassificationRange('<= 1', float('-inf'), 1.0)] + cla)

                # create ceiled (integers as limits) ranges
                lay_classes = lays_classes[qta_key] = []
                for lay_class in lay_classes_float:
                    if lay_class.lowerBound() == float('-inf'):
                        lay_classes.append(lay_class)
                        last_upper = int(lay_class.upperBound())  # 1.0
                        continue

                    upper = math.ceil(lay_class.upperBound())
                    if upper == last_upper:
                        # if current class fit into the same ceiled upper-bounded
                        # range it will be skipped
                        continue

                    lower = last_upper
                    lay_classes.append(QgsClassificationRange(
                        "%s - %s" % (
                            f'{lower:,}', f'{upper:,}'),
                        lower, upper))
                    last_upper = upper

            default_qgs_style = QgsStyle().defaultStyle()
            default_color_ramp_names = default_qgs_style.colorRampNames()
            real_lays = []
            for qta_key, qta in quantities.items():
                lay_classes = lays_classes[qta_key]
                qta['lay'].startEditing()
                qta['lay'].selectAll()

                qta['lay'].updateExtents()
                gpkg_filepath = '%s/%s_%s.gpkg' % (
                    layer_folder, qta_key, rnd_sfx)

                gem_log('calc2map: pre layer save [%s]' % gpkg_filepath,
                        Qgis.Critical)
                qta['lay'].commitChanges()
                QgsVectorFileWriter.writeAsVectorFormat(
                    qta['lay'], gpkg_filepath,
                    "UTF-8", qta['lay'].crs(), "GPKG",
                    layerOptions=['OVERWRITE=YES'])
                print('calc2map: post layer save')

                out_real_layer = QgsVectorLayer(gpkg_filepath, qta['descr'], 'ogr')
                real_lays.append(out_real_layer)
                symbol = QgsSymbol.defaultSymbol(out_real_layer.geometryType())
                symbol.setOpacity(1)
                ramp_type_idx = default_color_ramp_names.index(qta['ramp_col'])
                symbol.setColor(QColor(RAMP_EXTREME_COLORS[qta['ramp_col']]['top']))

                ramp = default_qgs_style.colorRamp(
                    default_color_ramp_names[ramp_type_idx])

                # ramp.invert() (to switch colors)

                # NOTE: get unique values not managed currently, check later
                # fni = out_real_layer.fields().indexOf('value')
                # unique_values = out_real_layer.dataProvider().uniqueValues(fni)
                # num_unique_values = len(unique_values - {NULL})

                # add a class for NULL values
                rule_renderer = QgsRuleBasedRenderer(symbol.clone())
                root_rule = rule_renderer.rootRule()
                gem_log('calc2map: len root_rule.children: %d' % len(
                    root_rule.children()), Qgis.Critical)

                gem_log('calc2map: pre loop lay_classes(%d)' % len(lay_classes),
                        Qgis.Critical)
                for cla_idx, cla in enumerate(lay_classes):
                    # filter = '"value" >= 0.000000 AND "value" <= 0.020361'
                    if cla.lowerBound() == float('-inf'):
                        upper = cla.upperBound()
                        filter = '"value" <= %s' % upper
                    else:
                        lower = cla.lowerBound()
                        upper = cla.upperBound()
                        filter = '"value" > %s AND "value" <= %s' % (
                            lower, upper)
                    cla_rule = rule_renderer.Rule(symbol.clone())

                    # ramp.color(0-1 included float)
                    color = ramp.color(
                        float(cla_idx) / float(len(lay_classes) - 1))
                    cla_rule.setSymbol(QgsFillSymbol.createSimple(
                    {'color':
                     '%d,%d,%d' % (color.red(), color.green(), color.blue())}))
                    cla_rule.setFilterExpression(filter)

                    # not_null_rule = root_rule.children()[0].clone()
                    # strip parentheses from stringified color HSL
                    # not_null_rule.setFilterExpression(
                    # '%s IS NOT NULL' % QgsExpression.quotedColumnRef(style_by))
                    cla_rule.setLabel(cla.label())
                    root_rule.appendChild(cla_rule)
                root_rule.removeChildAt(0)
                renderer = rule_renderer

                out_real_layer.setRenderer(renderer)
                out_real_layer.setId(qta_key + '_qgis_id')
                project.addMapLayer(out_real_layer)

                extent = out_real_layer.extent()
                gem_log('calc2map: extent of %s: %s' % (qta['descr'], extent), Qgis.Critical)

                ref_rect = QgsReferencedRectangle(extent, out_real_layer.crs())
                vs_project = project.viewSettings()
                vs_project.setDefaultViewExtent(ref_rect)

            gem_log('calc2map: pre project save', Qgis.Critical)

            # Create canvas
            canvas = QgsMapCanvas()
            canvas.setObjectName("theMapCanvas")
            # Set canvas size
            canvas.resize(QSize(800, 600))

            # FIXME: set proper values

            # Set coordinate reference system
            # crs = QgsCoordinateReferenceSystem("EPSG:4326")
            canvas.setDestinationCrs(project.crs())

            # Set extent
            # extent = ref_rect  # QgsRectangle(-180, -90, 180, 90)

            layer_extent = extent
            source_crs = qta['lay'].crs()
            dest_crs = project.crs()
            transform = QgsCoordinateTransform(source_crs, dest_crs, project)
            transformed_extent = transform.transformBoundingBox(layer_extent)
            canvas.setExtent(transformed_extent)

            canvas.refresh()

            #
            #  HOWTO: set project custom vars:
            #
            custom_vars = project.customVariables()
            custom_vars['project_info'] = json.dumps(proj_info)
            project.setCustomVariables(custom_vars)

            #
            #  save qgis project
            #
            project_filepath = '%s/%s.qgs' % (project_folder, project_name)
            project.write(project_filepath)

            project.clear()

            # gem_log('calc2map: post project save, filename [%s]' %
            #         project_filename, Qgis.Critical)
            old_dir = os.getcwd()
            os.chdir(project_folder)

            gem_log('calc2map: chdir("%s")' % project_folder, Qgis.Critical)

            archive_pathname = '%s.zip' % project_folder
            zipdir(archive_pathname, '.')
            os.chdir(old_dir)

            if os.getenv('GEM_GV_KEEP_CALC_PROJECT', False) is False:
                rmdir_recursive(project_folder)

            gem_log('calc2map: post project zip', Qgis.Critical)

            response.setStatusCode(200)
            response.write(
                json.dumps({'owner': 'mop',
                            'uploaded_file': os.path.basename(
                                archive_pathname)},
                           indent=4, sort_keys=True))

        except Exception as e:
            gem_log('calc2map: general exception occurred: %s' % e,
                    Qgis.Critical)
            raise e

        finally:
            if os.path.exists(fp_out.name):
                os.unlink(fp_out.name)
            release_lock(lock_filename)
        return;



class EWM():

    def __init__(self, serverIface):
        self.serv = EWMS()
        serverIface.serviceRegistry().registerService(EWMS())
