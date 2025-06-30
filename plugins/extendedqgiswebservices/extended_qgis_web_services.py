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
import os
import numpy
import json
from requests import Session

from qgis.core import Qgis
from qgis.server import QgsService, QgsServerProjectUtils
from qgis.core import (
    QgsRasterLayer, QgsProject, QgsVectorLayer, QgsVectorFileWriter,
    QgsField, edit, QgsFeature, QgsPointXY, QgsGeometry,
    QgsReferencedRectangle, QgsSymbol, QgsGradientColorRamp,
    QgsGraduatedSymbolRenderer, QgsCoordinateTransform,
    QgsApplication, QgsStyle, NULL,
    QgsProcessingFeedback, QgsProcessingContext,
    QgsProcessingAlgRunnerTask, QgsWkbTypes,
    QgsFeatureRequest, QgsProcessingFeatureSourceDefinition,
)


from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtCore import QVariant, QMetaType, QSize
from qgis.gui import QgsMapCanvas

import processing

from .svir_utils.shared import RAMP_EXTREME_COLORS
from .svir_utils.utils import get_style

# FIXME reenable folder deletion after zip creation
from .gem_common import gem_log, rmdir_recursive, alphanum_rndstr
from .lock import acquire_lock, release_lock
from .zipdir import zipdir


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
        elif request.parameters()['REQUEST'] == 'GetLayerCustomProperties':
            try:
                self._get_custom_properties_by_layer(
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
        elif request.parameters()['REQUEST'] == 'OQEngineGroupingTest':
            try:
                self._oq_engine_grouping_test(request, response, project)
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

    def _get_custom_properties_by_layer(self, request, response, project):
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
            layer_name = layer.shortName() or layer.name()
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
            layer_name = layer.shortName() or layer.name()
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
            layer_name = layer.shortName() or layer.name()
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

    def _oq_engine_grouping_test(self, request, response, project):
        project = QgsProject.instance()

        def create_layer_from_selected(source_layer):
            """Create a new layer containing only selected features"""
            if source_layer.selectedFeatureCount() == 0:
                print("No features selected!")
                return None

            # Create memory layer with same CRS and geometry type
            geom_type = QgsWkbTypes.displayString(source_layer.wkbType())
            crs = source_layer.crs().authid()
            temp_layer = QgsVectorLayer(f'{geom_type}?crs={crs}', 'selected_features', 'memory')

            # Copy selected features
            temp_provider = temp_layer.dataProvider()
            selected_features = list(source_layer.getSelectedFeatures())
            temp_provider.addFeatures(selected_features)
            temp_layer.updateExtents()

            return temp_layer



        # Clean current project
        project.clear()

        for i in range(0, 5):
            # rnd_sfx = alphanum_rndstr(8)
            description = 'hardcabled'
            rnd_sfx = 'notrandom'
            project_name = '%s_%s' % (description, rnd_sfx)
            # FIXME
            # lock_filename = '/io/uploads/projects/%s.lock' % project_name
            lock_filename = '/home/nastasi/git/oq-geoviewer/oqgeoviewer/media/uploads/projects/%s.lock' % project_name
            if acquire_lock(lock_filename):
                break
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

        # try:
        
        # Load another project
        # FIXME project.read('/io/data/_templates/PapersTmpl.qgs')
        project.read('/home/nastasi/git/oq-geoviewer/oqgeoviewer/data/qgis/_templates/Papers/PapersTmpl.qgs')
        gem_log('calc2map: project name: %s' % project.fileName(),
                Qgis.Critical)

        # mkdir of base for all files of the project
        # FIXME project_folder = '/io/uploads/projects/%s' % project_name
        project_folder = '/home/nastasi/git/oq-geoviewer/oqgeoviewer/media/uploads/projects/%s' % project_name
        layer_folder = '%s/layers' % project_folder
        try:
            os.mkdir(project_folder)
        except FileExistsError:
            pass
        try:
            os.mkdir(layer_folder)
        except FileExistsError:
            pass

        # except Exception:
        #     response.setStatusCode(500)
        #    response.write('grouptest: error')
        #     return


        
        response.setStatusCode(200)
        if processing is None:
            print('Processing is None')
        else:
            print('Processing available')

        processing.Processing.initialize()
        alg = QgsApplication.processingRegistry().algorithmById(
            'qgis:joinbylocationsummary')
        if alg is None:
            print('Alg is None')
        else:
            print('Alg available')

        context = QgsProcessingContext()
        feedback = QgsProcessingFeedback()

        points_uri = (
            '/home/nastasi/git/oq-geoviewer/project_samples/papers/'
            'avg_damages_mean/layers/output-75-avg_damages-mean_13.gpkg')
        
        points_layer = QgsVectorLayer(points_uri, 'emilia-romagna-10-PGA',
                                      'ogr')
        
        zonal_uri = ('/home/nastasi/git/oq-geoviewer/oqgeoviewer/media/'
                     'uploads/subdivision_areas/italy_adm2.gpkg')
        
        zonal_layer = QgsVectorLayer(zonal_uri, 'italy_adm2', 'ogr')

        # zonal_layer.selectAll()
        zonal_layer.removeSelection()

        complete_damage_lay = QgsVectorLayer(
            'Polygon?crs=epsg:3857', 'complete_damage', 'memory')
        complete_damage_dp = complete_damage_lay.dataProvider()
        complete_damage_lay.startEditing()
        complete_damage_lay.addAttribute(QgsField('value', QMetaType.Type.Double))
        complete_damage_lay.addAttribute(QgsField('json_info', QMetaType.Type.QString))
        complete_damage_lay.commitChanges()

        economic_losses_lay = QgsVectorLayer(
            'Polygon?crs=epsg:3857', 'economic_losses', 'memory')
        economic_losses_lay.startEditing()
        economic_losses_lay.addAttribute(QgsField('value', QMetaType.Type.Double))
        economic_losses_lay.addAttribute(QgsField('json_info', QMetaType.Type.QString))
        economic_losses_lay.commitChanges()
        economic_losses_dp = economic_losses_lay.dataProvider()
        
        fatalities_lay = QgsVectorLayer(
            'Polygon?crs=epsg:3857', 'fatalities', 'memory')
        fatalities_lay.startEditing()
        fatalities_lay.addAttribute(QgsField('value', QMetaType.Type.Double))
        fatalities_lay.addAttribute(QgsField('json_info', QMetaType.Type.QString))
        fatalities_lay.commitChanges()
        fatalities_dp = fatalities_lay.dataProvider()
        
        # create destination layer making a copy of regions layer and adding a
        # couple of fields


        # loop on group layer and, for each feature select layer points features and
        # process them
        for zonal_feat in zonal_layer.getFeatures():
            points_layer.removeSelection()
            zonal_layer.selectByIds([zonal_feat.id()])

            temp_layer = create_layer_from_selected(zonal_layer)

            result = processing.run("native:selectbylocation", {
                'INPUT': points_layer,
                'PREDICATE': [0],  # 0 = intersects
                'INTERSECT': temp_layer,
                # 'INTERSECT': QgsProcessingFeatureSourceDefinition(
                #     zonal_layer.dataProvider().dataSourceUri(),
                #     selectedFeaturesOnly=True,
                #     featureLimit=-1,
                #     geometryCheck=QgsFeatureRequest.GeometryAbortOnInvalid
                #   ),
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
            if len(points_layer.selectedFeatureIds()) > 0:
                print('N FEATS: %d' % len(
                    points_layer.selectedFeatureIds()))
                # loop to populate new entry for metrics here

                complete_damage_sum = 0
                economic_losses_sum = 0
                fatalities_sum = 0

                for feat in points_layer.selectedFeatures():
                    # print([x for x in feat])
                    # FIXME: avoid with a set() use the same point more than one time
                    complete_damage_sum += feat['structural-complete']
                    economic_losses_sum += feat['structural-losses']
                    fatalities_sum += feat['structural-fatalities']
                # print(f"cdam: {complete_damage_sum}, ecloss: {economic_losses_sum},"
                #       f" fatal: {fatalities_sum}")

                for out_lay, out_dp, out_sum, out_name in [
                        (complete_damage_lay, complete_damage_dp, complete_damage_sum, 'complete_damage'),
                        (economic_losses_lay, economic_losses_dp, economic_losses_sum, 'economic_losses'),
                        (fatalities_lay, fatalities_dp, fatalities_sum, 'fatalities')]:
                    if out_sum == 0.0:
                        continue
                    with edit(out_lay):
                        fea = QgsFeature(out_lay.fields())
                        fea.setGeometry(zonal_feat.geometry())
                        fea.setAttributes([float(out_sum), 'json_todo'])
                        out_dp.addFeatures([fea])
            else:
                print('N FEATS: ZERO')
            zonal_layer.removeSelection()

        for out_lay, out_name in [
                (complete_damage_lay, 'complete_damage'),
                (economic_losses_lay, 'economic_losses'),
                (fatalities_lay, 'fatalities')]:
            out_lay.selectAll()
            # Save layer as GeoPackage
            # save_options = QgsVectorFileWriter.SaveVectorOptions()
            # save_options.driverName = "GPKG"
            layer_name = f'{out_name}_plus'
            # save_options.layerName = f'{out_name}_adm2'

            out_lay.updateExtents()
            
            gpkg_filepath = '%s/%s_%s.gpkg' % (
                layer_folder, out_name, rnd_sfx)

            # gpkg_filepath = (
            #     f'/home/nastasi/git/oq-geoviewer/oqgeoviewer/media/'
            #     f'uploads/{out_name}_adm2.gpkg')

            gem_log('calc2map: pre layer save [%s]' % gpkg_filepath,
                    Qgis.Critical)
            error = QgsVectorFileWriter.writeAsVectorFormat(
                out_lay, gpkg_filepath,
                "UTF-8", out_lay.crs(), "GPKG",
                layerOptions=['OVERWRITE=YES'])
            print('calc2map: post layer save')





            out_real_layer = QgsVectorLayer(gpkg_filepath, out_name, 'ogr')

            # _style_curves(out_real_layer, out_name)

                # add gpkg layer to current QGIS project
            project.addMapLayer(out_real_layer)

            extent = out_real_layer.extent()
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
        source_crs = out_lay.crs()
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

        response.write(
            json.dumps({'status': 'success'}, indent=4, sort_keys=True))

    def _oq_engine_calc2map(self, request, response, project):
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

        # hostname = 'http://127.0.0.1:8800'
        eng_proto = 'http'
        eng_name = 'host.docker.internal'
        eng_port = '8800'
        engine_url = '%s://%s:%s' % (eng_proto, eng_name, eng_port)

        session = Session()

        # engine_login(hostname, None, None, session)

        gem_log('calc2map: pre get', Qgis.Critical)
        gem_log('calc2map: [%s]' % ('%s/v1/calc/list' % engine_url),
                Qgis.Critical)
        # retrieve list of calculations
        resp = session.get(
            '%s/v1/calc/list' % engine_url, timeout=10, verify=False,
            allow_redirects=False)

        gem_log('calc2map: post get', Qgis.Critical)

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
                    # QgsField("id", QVariant.Int),
                    QgsField(imt, QMetaType.Type.Double)
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

            # FIXME rmdir_recursive(project_folder)

            gem_log('calc2map: post project zip', Qgis.Critical)

            response.setStatusCode(200)
            response.write(
                json.dumps({'owner': 'mop',
                            'uploaded_file': os.path.basename(
                                archive_pathname)},
                           indent=4, sort_keys=True))
        finally:
            release_lock(lock_filename)


class EWM():

    def __init__(self, serverIface):
        self.serv = EWMS()
        serverIface.serviceRegistry().registerService(EWMS())
