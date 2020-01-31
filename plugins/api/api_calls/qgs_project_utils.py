import json
from qgis.core import (
    QgsProject, QgsUnitTypes, QgsVectorLayer, QgsRasterLayer, QgsDataSourceUri)
from qgis.server import QgsServerProjectUtils

ANGULAR_UNIT_TYPES = (QgsUnitTypes.DistanceUnit.DistanceDegrees, )
LINEAR_UNIT_TYPES = (
    QgsUnitTypes.DistanceUnit.DistanceFeet,
    QgsUnitTypes.DistanceUnit.DistanceNauticalMiles,
    QgsUnitTypes.DistanceUnit.DistanceYards,
    QgsUnitTypes.DistanceUnit.DistanceMiles,
    QgsUnitTypes.DistanceUnit.DistanceMillimeters,
    QgsUnitTypes.DistanceUnit.DistanceCentimeters,
    QgsUnitTypes.DistanceUnit.DistanceMeters,
    QgsUnitTypes.DistanceUnit.DistanceKilometers,)
UNKNOWN_UNIT_TYPES = (QgsUnitTypes.DistanceUnit.DistanceUnknownUnit, )


class ApiError(RuntimeError):
    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors


# Dynamic API generation #####################################################
# All functions in this module MUST have a first positional parameter called
# MAP. This is the path to the qgs file that is being edited. It is the
# content of the MAP url parameter
# all other parameters are poassed as kwargs from ApiService.executeRequest
# ############################################################################

def simple_test(MAP, name, provider='wms'):
    if name == 'mini-me':
        raise ApiError("I'm Austin Powers, I got you")
    return MAP, name, provider


def add_raster_layer(MAP, url, name, shortname, is_basemap, provider='wms'):
    """Add a raster layer to the project,
    path: path of the project, relative to the projects' directory

    Return: id of the added layer
    """

    layer = QgsRasterLayer(url, name, provider)
    layer.setShortName(shortname)

    project = QgsProject()
    project.read(MAP)

    project = QgsProject.instance()

    add_to_legend = not is_basemap
    added_layer = project.addMapLayer(layer, addToLegend=add_to_legend)

    project.write()
    return added_layer.id()


def add_vector_layer(MAP, url, name, shortname, is_basemap, provider='ogr'):
    """Add a vector layer to the project,
    path: path of the project, relative to the projects' directory

    Return: id of the added layer
    """
    layer = QgsVectorLayer(url, name, provider)
    layer.setShortName(shortname)

    project = QgsProject()
    project.read(MAP)

    project = QgsProject.instance()

    add_to_legend = not is_basemap
    added_layer = project.addMapLayer(layer, addToLegend=add_to_legend)

    project.write()
    return json.dumps(str(project.mapLayers()))
    return added_layer.id()


def remove_layer(MAP, layer_id):
    project = QgsProject()
    project.read(MAP)
    project = QgsProject.instance()
    project.removeMapLayer(layer_id)
    project.write()


def update_layer(
        MAP, layer_id, url=None, name=None, shortname=None):
    if url is None and name is None and shortname is None:
        raise ValueError("Please specify either url, name or shortname")
    project = QgsProject()
    project.read(MAP)
    project = QgsProject.instance()
    layer = project.mapLayer(layer_id)
    if url is not None:
        uri = QgsDataSourceUri(url)
        layer.dataProvider().setUri(uri)
    if name is not None:
        layer.setName(name)
    if shortname is not None:
        layer.setShortName(shortname)
    project.write()
    return json.dumps({'layer_id': layer.id(),
                       'url': str(layer.dataProvider().uri()),
                       'name': layer.name(),
                       'shortname': layer.shortName()})


def get_project_extent(MAP):
    project = QgsProject()
    project.read(MAP)
    project = QgsProject.instance()
    rect = QgsServerProjectUtils.wmsExtent(project)
    map_unit = project.crs().mapUnits()
    if map_unit in ANGULAR_UNIT_TYPES:
        unit_type = 'angular'
    elif map_unit in LINEAR_UNIT_TYPES:
        unit_type = 'linear'
    else:
        unit_type = 'unknown'
    return json.dumps({'xmin': rect.xMinimum(),
                       'ymin': rect.yMinimum(),
                       'xmax': rect.xMaximum(),
                       'ymax': rect.yMaximum(),
                       'map_unit': map_unit,
                       'unit_type': unit_type})


if __name__ == '__main__':
    import requests
    urls = {}
    base_url = 'http://localhost:8010/ogc/nepal_hazard?service=API'
    urls[base_url + '&request=simple_test&name=good'] = 200
    urls[base_url + '&request=simple_test&name=mini-me'] = 500
    urls[base_url + '&request=simple_test'] = 500
    urls[(base_url +
          '&request=add_raster_layer'
          '&url=type%3Dxyz%26%26url%3Dhttp%3A%2F%2Fbasemaps.cartocdn.com%2Fdark_all%2F%7Bz%7D%2F%7Bx%7D%2F%7By%7D.png'
          '&name=CartoDb%20Dark%20Matter'
          '&shortname=cartodb-dark-matter'
          '&is_basemap=true')
         ] = 200
    for url, status in urls.items():
        print(url)
        response = requests.get(url)
        if response.status_code != status:
            raise RuntimeError('Expected status code {}, got {}'.format(
                status, response.status_code))
        print(response.text)
