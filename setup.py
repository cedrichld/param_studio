from setuptools import setup

package_name = 'param_studio'

setup(
    name=package_name,
    version='0.0.1',
    packages=[package_name],
    package_data={package_name: [
        'templates/*.html',
        'static/*.js',
        'static/*.css',
    ]},
    include_package_data=True,
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=False,
    maintainer='cedric',
    maintainer_email='cedrich@seas.upenn.edu',
    description='Modern offline rqt_reconfigure replacement with set read-back '
                'confirmation, snapshot/restore and connection health.',
    license='MIT',
    entry_points={
        'console_scripts': [
            'studio = param_studio.app:main',
        ],
    },
)
